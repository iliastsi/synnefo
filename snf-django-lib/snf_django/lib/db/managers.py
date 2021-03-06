# Copyright 2012, 2013 GRNET S.A. All rights reserved.
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

from django.db import connections
from django.db.models import Manager
from django.db.models.query import QuerySet, EmptyQuerySet
from django.db.models.sql.datastructures import EmptyResultSet

class ForUpdateManager(Manager):
    """ Model manager implementing SELECT .. FOR UPDATE statement

        This manager implements select_for_update() method in order to use
        row-level locking in the database and guarantee exclusive access, since
        this method is only implemented in Django>=1.4.

        Non-blocking reads are not implemented, and each query including a row
        that is locked by another transaction will block until the lock is
        released. Also care must be taken in order to avoid deadlocks or retry
        transactions that abort due to deadlocks.

        Example:
            networks = Network.objects.filter(public=True).select_for_update()

    """

    def get_query_set(self):
        return ForUpdateQuerySet(self.model, using=self._db)

    def get_for_update(self, *args, **kwargs):
        query = for_update(self.filter(*args, **kwargs))
        query = list(query)
        num = len(query)
        if num == 1:
            return query[0]
        if not num:
            raise self.model.DoesNotExist(
                    "%s matching query does not exist. "
                    "Lookup parameters were %s" %
                    (self.model._meta.object_name, kwargs))
        raise self.model.MultipleObjectsReturned(
            "get() returned more than one %s -- it returned %s! "
            "Lookup parameters were %s" %
            (self.model._meta.object_name, num, kwargs))


class ForUpdateQuerySet(QuerySet):

    def select_for_update(self):
        return for_update(self)


def for_update(query):
    """ Rewrite query using SELECT .. FOR UPDATE.

    """
    if 'sqlite' in connections[query.db].settings_dict['ENGINE'].lower():
        # SQLite  does not support FOR UPDATE
        return query
    try:
        sql, params = query.query.get_compiler(query.db).as_sql()
    except EmptyResultSet:
        return EmptyQuerySet()
    return query.model._default_manager.raw(sql.rstrip() + ' FOR UPDATE',
                                            params)
