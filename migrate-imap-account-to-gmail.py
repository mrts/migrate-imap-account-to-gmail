#!/usr/bin/env python
"""
Script that copies mail from a Dovecot IMAP server account to a GMail account.

`pip install imapclient` and create `conf.py` as follows to use it:

SOURCE = {
    'HOST': 'example.com',
    'USERNAME': 'user',
    'PASSWORD': 'password',
    'SSL': True,
}

TARGET = {
    'HOST': 'imap.gmail.com',
    'USERNAME': 'user@gmail.com',
    'PASSWORD': 'password',
    'SSL': True,
}
"""

from __future__ import unicode_literals
import time

from imapclient import IMAPClient

import conf

def main():
    source_account = Source(conf.SOURCE)
    target_account = Target(conf.TARGET)

    yes = raw_input("Copy all mail\n"
            "from account\n"
            "\t%s\n"
            "to account\n"
            "\t%s\n[yes/no]? " %
            (source_account, target_account))
    if yes != "yes":
        print("Didn't enter 'yes', exiting")
        return

    for folder in source_account.list_folders():
        print("Synchronizing folder '%s'" % folder)
        start = time.clock()
        target_account.create_folder(folder)
        folder_info = source_account.select_folder(folder)
        print("\tcontains %s messages" % folder_info['EXISTS'])
        messages = source_account.fetch_messages()
        for message, size in messages:
            print("\t\tuploading message of %s bytes" % size)
            target_account.append(folder, message)
        end = time.clock()
        print("\t'%s' done, took %s seconds" % (folder, end - start))

class Base(object):
    def __init__(self, conf):
        self.username = conf['USERNAME']
        self.host = conf['HOST']
        self.server = IMAPClient(conf['HOST'], use_uid=True, ssl=conf['SSL'])
        self.server.login(conf['USERNAME'], conf['PASSWORD'])

    def __str__(self):
        return "<user: %s | host: %s>" % (self.username, self.host)

class Source(Base):
    SPECIAL_FOLDERS_REMAP = {
        u'Drafts': u'special_folders.drafts',
        u'Junk': u'special_folders.junk',
        u'Sent': u'special_folders.sent',
        u'Trash': u'special_folders.trash',
        u'INBOX': u'special_folders.inbox',
        u'INBOX.Drafts': u'special_folders.inbox.drafts',
        u'INBOX.Junk': u'special_folders.inbox.junk',
        u'INBOX.Sent': u'special_folders.inbox.sent',
        u'INBOX.Trash': u'special_folders.inbox.trash',
        u'Mail.Drafts': u'special_folders.mail.drafts',
        u'Mail.Junk': u'special_folders.mail.junk',
        u'Mail.Sent': u'special_folders.mail.sent',
        u'Mail.Trash': u'special_folders.mail.trash',
    }

    def list_folders(self):
        for flags, root, folder in self.server.list_folders():
            if folder in self.SPECIAL_FOLDERS_REMAP:
                folder = self.SPECIAL_FOLDERS_REMAP[folder]
            yield folder
            raise StopIteration

    def select_folder(self, folder):
        return self.server.select_folder(folder)

    def fetch_messages(self):
        messages = self.server.search(['NOT DELETED'])
        # ['FLAGS'] and pass flags to target account?
        response = self.server.fetch(messages, ['RFC822', 'RFC822.SIZE'])
        for msgid, data in response.iteritems():
            yield data['RFC822'], data['RFC822.SIZE']

class Target(Base):
    def create_folder(self, folder):
        pass
        # folder = folder.replace('.', '/')
        # self.server.create_folder(folder)

    def append(self, folder, message):
        pass
        # self.server.append(folder, message)

if __name__ == '__main__':
    main()
