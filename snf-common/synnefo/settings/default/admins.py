# -*- coding: utf-8 -*-
#
# Admin names and email addresses
##################################

# List of people who receive application notifications, such as code error
# tracebacks. It is recommended to have at least one entry in this list.
ADMINS = (
    # ('Your Name', 'your_email@domain.com'),
)

# List of people to receive user feedback notifications.
HELPDESK = (
    # ('Your Name', 'your_email@domain.com'),
)

# A list of people to receive email notifications on some application events
# (e.g. account creation/activation).
MANAGERS = (
    # ('Your Name', 'your_email@domain.com'),
)

# Email configuration
EMAIL_HOST = "127.0.0.1"
EMAIL_HOST_USER = ""
EMAIL_HOST_PASSWORD = ""
EMAIL_SUBJECT_PREFIX = "[email-subject-prefix] "
DEFAULT_CHARSET = 'utf-8'

# Address to use for outgoing emails
DEFAULT_FROM_EMAIL = "synnefo <no-reply@synnefo.org>"

# Email where users can contact for support
CONTACT_EMAIL = "support@synnefo.org"

# Email address the emails sent by the service will come from
SERVER_EMAIL = "Synnefo cloud <cloud@synnefo.org>"
