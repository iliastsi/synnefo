# Copyright 2011-2012 GRNET S.A. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#   1. Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright
#     notice, this list of conditions and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE REGENTS AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official
# policies, either expressed or implied, of GRNET S.A.

import datetime

from django.conf import settings
from django.db import models
from django.db import IntegrityError
from django.db import transaction

import utils
from contextlib import contextmanager
from hashlib import sha1
from synnefo.api.faults import ServiceUnavailable
from synnefo import settings as snf_settings
from aes_encrypt import encrypt_db_charfield, decrypt_db_charfield

from synnefo.db.managers import ForUpdateManager, ProtectedDeleteManager
from synnefo.db import pools

from synnefo.logic.rapi_pool import (get_rapi_client,
                                     put_rapi_client)

import logging
log = logging.getLogger(__name__)


class Flavor(models.Model):
    cpu = models.IntegerField('Number of CPUs', default=0)
    ram = models.IntegerField('RAM size in MiB', default=0)
    disk = models.IntegerField('Disk size in GiB', default=0)
    disk_template = models.CharField('Disk template', max_length=32,
            default=settings.DEFAULT_GANETI_DISK_TEMPLATE)
    deleted = models.BooleanField('Deleted', default=False)

    class Meta:
        verbose_name = u'Virtual machine flavor'
        unique_together = ('cpu', 'ram', 'disk', 'disk_template')

    @property
    def name(self):
        """Returns flavor name (generated)"""
        return u'C%dR%dD%d' % (self.cpu, self.ram, self.disk)

    def __unicode__(self):
        return str(self.id)


class Backend(models.Model):
    clustername = models.CharField('Cluster Name', max_length=128, unique=True)
    port = models.PositiveIntegerField('Port', default=5080)
    username = models.CharField('Username', max_length=64, blank=True,
                                null=True)
    password_hash = models.CharField('Password', max_length=128, blank=True,
                                null=True)
    # Sha1 is up to 40 characters long
    hash = models.CharField('Hash', max_length=40, editable=False, null=False)
    # Unique index of the Backend, used for the mac-prefixes of the
    # BackendNetworks
    index = models.PositiveIntegerField('Index', null=False, unique=True,
                                        default=0)
    drained = models.BooleanField('Drained', default=False, null=False)
    offline = models.BooleanField('Offline', default=False, null=False)
    # Last refresh of backend resources
    updated = models.DateTimeField(auto_now_add=True)
    # Backend resources
    mfree = models.PositiveIntegerField('Free Memory', default=0, null=False)
    mtotal = models.PositiveIntegerField('Total Memory', default=0, null=False)
    dfree = models.PositiveIntegerField('Free Disk', default=0, null=False)
    dtotal = models.PositiveIntegerField('Total Disk', default=0, null=False)
    pinst_cnt = models.PositiveIntegerField('Primary Instances', default=0,
                                            null=False)
    ctotal = models.PositiveIntegerField('Total number of logical processors',
                                         default=0, null=False)
    # Custom object manager to protect from cascade delete
    objects = ProtectedDeleteManager()

    class Meta:
        verbose_name = u'Backend'
        ordering = ["clustername"]

    def __unicode__(self):
        return self.clustername + "(id=" + str(self.id) + ")"

    @property
    def backend_id(self):
        return self.id

    def get_client(self):
        """Get or create a client. """
        if self.offline:
            raise ServiceUnavailable
        return get_rapi_client(self.id, self.hash,
                               self.clustername,
                               self.port,
                               self.username,
                               self.password)

    @staticmethod
    def put_client(client):
            put_rapi_client(client)

    def create_hash(self):
        """Create a hash for this backend. """
        return sha1('%s%s%s%s' % \
                (self.clustername, self.port, self.username, self.password)) \
                .hexdigest()

    @property
    def password(self):
        return decrypt_db_charfield(self.password_hash)

    @password.setter
    def password(self, value):
        self.password_hash = encrypt_db_charfield(value)

    def save(self, *args, **kwargs):
        # Create a new hash each time a Backend is saved
        old_hash = self.hash
        self.hash = self.create_hash()
        super(Backend, self).save(*args, **kwargs)
        if self.hash != old_hash:
            # Populate the new hash to the new instances
            self.virtual_machines.filter(deleted=False).update(backend_hash=self.hash)

    def delete(self, *args, **kwargs):
        # Integrity Error if non-deleted VMs are associated with Backend
        if self.virtual_machines.filter(deleted=False).count():
            raise IntegrityError("Non-deleted virtual machines are associated "
                                 "with backend: %s" % self)
        else:
            # ON_DELETE = SET NULL
            self.virtual_machines.all().backend = None
            super(Backend, self).delete(*args, **kwargs)

    def __init__(self, *args, **kwargs):
        super(Backend, self).__init__(*args, **kwargs)
        if not self.pk:
            # Generate a unique index for the Backend
            indexes = Backend.objects.all().values_list('index', flat=True)
            first_free = [x for x in xrange(0, 16) if x not in indexes][0]
            self.index = first_free


# A backend job may be in one of the following possible states
BACKEND_STATUSES = (
    ('queued', 'request queued'),
    ('waiting', 'request waiting for locks'),
    ('canceling', 'request being canceled'),
    ('running', 'request running'),
    ('canceled', 'request canceled'),
    ('success', 'request completed successfully'),
    ('error', 'request returned error')
)


class VirtualMachine(models.Model):
    # The list of possible actions for a VM
    ACTIONS = (
       ('CREATE', 'Create VM'),
       ('START', 'Start VM'),
       ('STOP', 'Shutdown VM'),
       ('SUSPEND', 'Admin Suspend VM'),
       ('REBOOT', 'Reboot VM'),
       ('DESTROY', 'Destroy VM')
    )

    # The internal operating state of a VM
    OPER_STATES = (
        ('BUILD', 'Queued for creation'),
        ('ERROR', 'Creation failed'),
        ('STOPPED', 'Stopped'),
        ('STARTED', 'Started'),
        ('DESTROYED', 'Destroyed')
    )

    # The list of possible operations on the backend
    BACKEND_OPCODES = (
        ('OP_INSTANCE_CREATE', 'Create Instance'),
        ('OP_INSTANCE_REMOVE', 'Remove Instance'),
        ('OP_INSTANCE_STARTUP', 'Startup Instance'),
        ('OP_INSTANCE_SHUTDOWN', 'Shutdown Instance'),
        ('OP_INSTANCE_REBOOT', 'Reboot Instance'),

        # These are listed here for completeness,
        # and are ignored for the time being
        ('OP_INSTANCE_SET_PARAMS', 'Set Instance Parameters'),
        ('OP_INSTANCE_QUERY_DATA', 'Query Instance Data'),
        ('OP_INSTANCE_REINSTALL', 'Reinstall Instance'),
        ('OP_INSTANCE_ACTIVATE_DISKS', 'Activate Disks'),
        ('OP_INSTANCE_DEACTIVATE_DISKS', 'Deactivate Disks'),
        ('OP_INSTANCE_REPLACE_DISKS', 'Replace Disks'),
        ('OP_INSTANCE_MIGRATE', 'Migrate Instance'),
        ('OP_INSTANCE_CONSOLE', 'Get Instance Console'),
        ('OP_INSTANCE_RECREATE_DISKS', 'Recreate Disks'),
        ('OP_INSTANCE_FAILOVER', 'Failover Instance')
    )

    # The operating state of a VM,
    # upon the successful completion of a backend operation.
    # IMPORTANT: Make sure all keys have a corresponding
    # entry in BACKEND_OPCODES if you update this field, see #1035, #1111.
    OPER_STATE_FROM_OPCODE = {
        'OP_INSTANCE_CREATE': 'STARTED',
        'OP_INSTANCE_REMOVE': 'DESTROYED',
        'OP_INSTANCE_STARTUP': 'STARTED',
        'OP_INSTANCE_SHUTDOWN': 'STOPPED',
        'OP_INSTANCE_REBOOT': 'STARTED',
        'OP_INSTANCE_SET_PARAMS': None,
        'OP_INSTANCE_QUERY_DATA': None,
        'OP_INSTANCE_REINSTALL': None,
        'OP_INSTANCE_ACTIVATE_DISKS': None,
        'OP_INSTANCE_DEACTIVATE_DISKS': None,
        'OP_INSTANCE_REPLACE_DISKS': None,
        'OP_INSTANCE_MIGRATE': None,
        'OP_INSTANCE_CONSOLE': None,
        'OP_INSTANCE_RECREATE_DISKS': None,
        'OP_INSTANCE_FAILOVER': None
    }

    # This dictionary contains the correspondence between
    # internal operating states and Server States as defined
    # by the Rackspace API.
    RSAPI_STATE_FROM_OPER_STATE = {
        "BUILD": "BUILD",
        "ERROR": "ERROR",
        "STOPPED": "STOPPED",
        "STARTED": "ACTIVE",
        "DESTROYED": "DELETED"
    }

    name = models.CharField('Virtual Machine Name', max_length=255)
    userid = models.CharField('User ID of the owner', max_length=100)
    backend = models.ForeignKey(Backend, null=True,
                                related_name="virtual_machines",)
    backend_hash = models.CharField(max_length=128, null=True, editable=False)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    imageid = models.CharField(max_length=100, null=False)
    hostid = models.CharField(max_length=100)
    flavor = models.ForeignKey(Flavor)
    deleted = models.BooleanField('Deleted', default=False)
    suspended = models.BooleanField('Administratively Suspended',
                                    default=False)

    # VM State
    # The following fields are volatile data, in the sense
    # that they need not be persistent in the DB, but rather
    # get generated at runtime by quering Ganeti and applying
    # updates received from Ganeti.

    # In the future they could be moved to a separate caching layer
    # and removed from the database.
    # [vkoukis] after discussion with [faidon].
    action = models.CharField(choices=ACTIONS, max_length=30, null=True)
    operstate = models.CharField(choices=OPER_STATES, max_length=30, null=True)
    backendjobid = models.PositiveIntegerField(null=True)
    backendopcode = models.CharField(choices=BACKEND_OPCODES, max_length=30,
                                     null=True)
    backendjobstatus = models.CharField(choices=BACKEND_STATUSES,
                                        max_length=30, null=True)
    backendlogmsg = models.TextField(null=True)
    buildpercentage = models.IntegerField(default=0)
    backendtime = models.DateTimeField(default=datetime.datetime.min)

    def get_client(self):
        if self.backend:
            return self.backend.get_rapi_client()
        else:
            raise ServiceUnavailable

    @staticmethod
    def put_client(client):
            put_rapi_client(client)

    # Error classes
    class InvalidBackendIdError(Exception):
        def __init__(self, value):
            self.value = value

        def __str__(self):
            return repr(self.value)

    class InvalidBackendMsgError(Exception):
        def __init__(self, opcode, status):
            self.opcode = opcode
            self.status = status

        def __str__(self):
            return repr('<opcode: %s, status: %s>' % (self.opcode,
                        self.status))

    class InvalidActionError(Exception):
        def __init__(self, action):
            self._action = action

        def __str__(self):
            return repr(str(self._action))

    class DeletedError(Exception):
        pass

    class BuildingError(Exception):
        pass

    def __init__(self, *args, **kw):
        """Initialize state for just created VM instances."""
        super(VirtualMachine, self).__init__(*args, **kw)
        # This gets called BEFORE an instance gets save()d for
        # the first time.
        if not self.pk:
            self.action = None
            self.backendjobid = None
            self.backendjobstatus = None
            self.backendopcode = None
            self.backendlogmsg = None
            self.operstate = 'BUILD'

    def save(self, *args, **kwargs):
        # Store hash for first time saved vm
        if (self.id is None or self.backend_hash == '') and self.backend:
            self.backend_hash = self.backend.hash
        super(VirtualMachine, self).save(*args, **kwargs)

    @property
    def backend_vm_id(self):
        """Returns the backend id for this VM by prepending backend-prefix."""
        if not self.id:
            raise VirtualMachine.InvalidBackendIdError("self.id is None")
        return "%s%s" % (settings.BACKEND_PREFIX_ID, str(self.id))

    class Meta:
        verbose_name = u'Virtual machine instance'
        get_latest_by = 'created'

    def __unicode__(self):
        return str(self.id)


class VirtualMachineMetadata(models.Model):
    meta_key = models.CharField(max_length=50)
    meta_value = models.CharField(max_length=500)
    vm = models.ForeignKey(VirtualMachine, related_name='metadata')

    class Meta:
        unique_together = (('meta_key', 'vm'),)
        verbose_name = u'Key-value pair of metadata for a VM.'

    def __unicode__(self):
        return u'%s: %s' % (self.meta_key, self.meta_value)


class Network(models.Model):
    OPER_STATES = (
        ('PENDING', 'Pending'),
        ('ACTIVE', 'Active'),
        ('DELETED', 'Deleted'),
        ('ERROR', 'Error')
    )

    ACTIONS = (
       ('CREATE', 'Create Network'),
       ('DESTROY', 'Destroy Network'),
    )

    RSAPI_STATE_FROM_OPER_STATE = {
        'PENDING': 'PENDING',
        'ACTIVE': 'ACTIVE',
        'DELETED': 'DELETED',
        'ERROR': 'ERROR'
    }

    NETWORK_TYPES = (
        ('PUBLIC_ROUTED', 'Public routed network'),
        ('PRIVATE_PHYSICAL_VLAN', 'Private vlan network'),
        ('PRIVATE_MAC_FILTERED', 'Private network with mac-filtering'),
        ('CUSTOM_ROUTED', 'Custom routed network'),
        ('CUSTOM_BRIDGED', 'Custom bridged network')
    )

    name = models.CharField('Network Name', max_length=128)
    userid = models.CharField('User ID of the owner', max_length=128, null=True)
    subnet = models.CharField('Subnet', max_length=32, default='10.0.0.0/24')
    subnet6 = models.CharField('IPv6 Subnet', max_length=64, null=True)
    gateway = models.CharField('Gateway', max_length=32, null=True)
    gateway6 = models.CharField('IPv6 Gateway', max_length=64, null=True)
    dhcp = models.BooleanField('DHCP', default=True)
    type = models.CharField(choices=NETWORK_TYPES, max_length=50,
                            default='PRIVATE_PHYSICAL_VLAN')
    link = models.CharField('Network Link', max_length=128, null=True)
    mac_prefix = models.CharField('MAC Prefix', max_length=32, null=False)
    public = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    deleted = models.BooleanField('Deleted', default=False)
    state = models.CharField(choices=OPER_STATES, max_length=32,
                             default='PENDING')
    machines = models.ManyToManyField(VirtualMachine,
                                      through='NetworkInterface')
    action = models.CharField(choices=ACTIONS, max_length=32, null=True,
                              default=None)

    pool = models.OneToOneField('IPPoolTable', related_name='network',
                                null=True)

    objects = ForUpdateManager()

    def __unicode__(self):
        return str(self.id)

    class InvalidBackendIdError(Exception):
        def __init__(self, value):
            self.value = value

        def __str__(self):
            return repr(self.value)

    class InvalidBackendMsgError(Exception):
        def __init__(self, opcode, status):
            self.opcode = opcode
            self.status = status

        def __str__(self):
            return repr('<opcode: %s, status: %s>' % (self.opcode,
                    self.status))

    class InvalidActionError(Exception):
        def __init__(self, action):
            self._action = action

        def __str__(self):
            return repr(str(self._action))

    @property
    def backend_id(self):
        """Return the backend id by prepending backend-prefix."""
        if not self.id:
            raise Network.InvalidBackendIdError("self.id is None")
        return "%snet-%s" % (settings.BACKEND_PREFIX_ID, str(self.id))

    @property
    def backend_tag(self):
        """Return the network tag to be used in backend

        """
        return getattr(snf_settings, self.type + '_TAGS')

    @transaction.commit_on_success
    def update_state(self):
        """Update state of the Network.

        Update the state of the Network depending on the related
        backend_networks. When backend networks do not have the same operstate,
        the Network's state is PENDING. Otherwise it is the same with
        the BackendNetworks operstate.

        """

        old_state = self.state

        backend_states = [s.operstate for s in self.backend_networks.all()]
        if not backend_states:
            self.state = 'PENDING'
            self.save()
            return

        all_equal = len(set(backend_states)) <= 1
        self.state = all_equal and backend_states[0] or 'PENDING'

        # Release the resources on the deletion of the Network
        if old_state != 'DELETED' and self.state == 'DELETED':
            log.info("Network %r deleted. Releasing link %r mac_prefix %r",
                     self.id, self.mac_prefix, self.link)
            self.deleted = True
            if self.mac_prefix:
                mac_pool = MacPrefixPoolTable.get_pool()
                mac_pool.put(self.mac_prefix)
                mac_pool.save()

            if self.link and self.type == 'PRIVATE_VLAN':
                bridge_pool = BridgePoolTable.get_pool()
                bridge_pool.put(self.link)
                bridge_pool.save()

        self.save()

    def create_backend_network(self, backend=None):
        """Create corresponding BackendNetwork entries."""

        backends = [backend] if backend else Backend.objects.all()
        for backend in backends:
            BackendNetwork.objects.create(backend=backend, network=self)

    def get_pool(self):
        if not self.pool_id:
            self.pool = IPPoolTable.objects.create(available_map='',
                                                   reserved_map='',
                                                   size=0)
            self.save()
        return IPPoolTable.objects.select_for_update().get(id=self.pool_id).pool

    def reserve_address(self, address):
        pool = self.get_pool()
        pool.reserve(address)
        pool.save()

    def release_address(self, address):
        pool = self.get_pool()
        pool.put(address)
        pool.save()


class BackendNetwork(models.Model):
    OPER_STATES = (
        ('PENDING', 'Pending'),
        ('ACTIVE', 'Active'),
        ('DELETED', 'Deleted'),
        ('ERROR', 'Error')
    )

    # The list of possible operations on the backend
    BACKEND_OPCODES = (
        ('OP_NETWORK_ADD', 'Create Network'),
        ('OP_NETWORK_CONNECT', 'Activate Network'),
        ('OP_NETWORK_DISCONNECT', 'Deactivate Network'),
        ('OP_NETWORK_REMOVE', 'Remove Network'),
        # These are listed here for completeness,
        # and are ignored for the time being
        ('OP_NETWORK_SET_PARAMS', 'Set Network Parameters'),
        ('OP_NETWORK_QUERY_DATA', 'Query Network Data')
    )

    # The operating state of a Netowork,
    # upon the successful completion of a backend operation.
    # IMPORTANT: Make sure all keys have a corresponding
    # entry in BACKEND_OPCODES if you update this field, see #1035, #1111.
    OPER_STATE_FROM_OPCODE = {
        'OP_NETWORK_ADD': 'PENDING',
        'OP_NETWORK_CONNECT': 'ACTIVE',
        'OP_NETWORK_DISCONNECT': 'PENDING',
        'OP_NETWORK_REMOVE': 'DELETED',
        'OP_NETWORK_SET_PARAMS': None,
        'OP_NETWORK_QUERY_DATA': None
    }

    network = models.ForeignKey(Network, related_name='backend_networks')
    backend = models.ForeignKey(Backend, related_name='networks')
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    deleted = models.BooleanField('Deleted', default=False)
    mac_prefix = models.CharField('MAC Prefix', max_length=32, null=False)
    operstate = models.CharField(choices=OPER_STATES, max_length=30,
                                 default='PENDING')
    backendjobid = models.PositiveIntegerField(null=True)
    backendopcode = models.CharField(choices=BACKEND_OPCODES, max_length=30,
                                     null=True)
    backendjobstatus = models.CharField(choices=BACKEND_STATUSES,
                                        max_length=30, null=True)
    backendlogmsg = models.TextField(null=True)
    backendtime = models.DateTimeField(null=False,
                                       default=datetime.datetime.min)

    class Meta:
        # Ensure one entry for each network in each backend
        unique_together = (("network", "backend"))

    def __init__(self, *args, **kwargs):
        """Initialize state for just created BackendNetwork instances."""
        super(BackendNetwork, self).__init__(*args, **kwargs)
        if not self.mac_prefix:
            # Generate the MAC prefix of the BackendNetwork, by combining
            # the Network prefix with the index of the Backend
            net_prefix = self.network.mac_prefix
            backend_suffix = hex(self.backend.index).replace('0x', '')
            mac_prefix = net_prefix + backend_suffix
            try:
                utils.validate_mac(mac_prefix + ":00:00:00")
            except utils.InvalidMacAddress:
                raise utils.InvalidMacAddress("Invalid MAC prefix '%s'" % \
                                               mac_prefix)
            self.mac_prefix = mac_prefix

    def save(self, *args, **kwargs):
        super(BackendNetwork, self).save(*args, **kwargs)
        self.network.update_state()

    def delete(self, *args, **kwargs):
        super(BackendNetwork, self).delete(*args, **kwargs)
        self.network.update_state()


class NetworkInterface(models.Model):
    FIREWALL_PROFILES = (
        ('ENABLED', 'Enabled'),
        ('DISABLED', 'Disabled'),
        ('PROTECTED', 'Protected')
    )

    machine = models.ForeignKey(VirtualMachine, related_name='nics')
    network = models.ForeignKey(Network, related_name='nics')
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    index = models.IntegerField(null=False)
    mac = models.CharField(max_length=32, null=False, unique=True)
    ipv4 = models.CharField(max_length=15, null=True)
    ipv6 = models.CharField(max_length=100, null=True)
    firewall_profile = models.CharField(choices=FIREWALL_PROFILES,
                                        max_length=30, null=True)
    dirty = models.BooleanField(default=False)

    def __unicode__(self):
        return '%s@%s' % (self.machine.name, self.network.name)


class PoolTable(models.Model):
    available_map = models.TextField(default="", null=False)
    reserved_map = models.TextField(default="", null=False)
    size = models.IntegerField(null=False)

    # Optional Fields
    base = models.CharField(null=True, max_length=32)
    offset = models.IntegerField(null=True)

    objects = ForUpdateManager()

    class Meta:
        abstract = True

    @classmethod
    def get_pool(cls):
        try:
            pool_row = cls.objects.select_for_update().all()[0]
            return pool_row.pool
        except IndexError:
            raise pools.EmptyPool

    @property
    def pool(self):
        return self.manager(self)


class BridgePoolTable(PoolTable):
    manager = pools.BridgePool


class MacPrefixPoolTable(PoolTable):
    manager = pools.MacPrefixPool


class IPPoolTable(PoolTable):
    manager = pools.IPPool


@contextmanager
def pooled_rapi_client(obj):
        if isinstance(obj, VirtualMachine):
            backend = obj.backend
        else:
            backend = obj

        if backend.offline:
            raise ServiceUnavailable

        b = backend
        client = get_rapi_client(b.id, b.hash, b.clustername, b.port,
                                 b.username, b.password)
        yield client
        put_rapi_client(client)
