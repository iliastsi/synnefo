#coding=utf8

USE_X_FORWARDED_HOST = False

# Either set local users here, or a remote host.
# To disable local users set to None.
AUTHENTICATION_USERS = {
    '0000': 'test',
    '0001': 'verigak',
    '0002': 'chazapis',
    '0003': 'gtsouk',
    '0004': 'papagian',
    '0005': 'louridas',
    '0006': 'chstath',
    '0007': 'pkanavos',
    '0008': 'mvasilak',
    '0009': 'διογένης'
}

# Where astakos is hosted.
AUTHENTICATION_HOST = '127.0.0.1:10000'

TEST = False
