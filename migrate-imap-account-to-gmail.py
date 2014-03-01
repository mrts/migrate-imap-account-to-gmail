#!/usr/bin/env python
# encoding: utf-8
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
import email
from email.generator import Generator as EmailGenerator
from cStringIO import StringIO

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
        start = time.time()
        target_folder = target_account.create_folder(folder)
        folder_info = source_account.select_folder(folder)
        print("\tcontains %s messages" % folder_info['EXISTS'])
        messages = source_account.fetch_messages()
        for message, flags, size in messages:
            print("\t\tuploading message of %s bytes to '%s'" %
                    (size, target_folder))
            target_account.append(target_folder, to_message(message), flags)
        end = time.time()
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
    def list_folders(self):
        return (folderinfo[2] for folderinfo in self.server.list_folders())

    def select_folder(self, folder):
        return self.server.select_folder(folder)

    def fetch_messages(self):
        messages = self.server.search(['NOT DELETED'])
        response = self.server.fetch(messages,
                ['FLAGS', 'RFC822', 'RFC822.SIZE'])
        return ((data['RFC822'], data['FLAGS'], data['RFC822.SIZE'])
                for msgid, data in response.iteritems())

class Target(Base):
    SPECIAL_FOLDERS_REMAP = {
        u'Drafts': u'specialfolders/drafts',
        u'Junk': u'specialfolders/junk',
        u'Sent': u'specialfolders/sent',
        u'Trash': u'specialfolders/trash',
        u'INBOX': u'specialfolders/inbox',
        u'INBOX/Drafts': u'specialfolders/inbox/drafts',
        u'INBOX/Junk': u'specialfolders/inbox/junk',
        u'INBOX/Sent': u'specialfolders/inbox/sent',
        u'INBOX/Trash': u'specialfolders/inbox/trash',
        u'Mail/Drafts': u'specialfolders/mail/drafts',
        u'Mail/Junk': u'specialfolders/mail/junk',
        u'Mail/Sent': u'specialfolders/mail/sent',
        u'Mail/Trash': u'specialfolders/mail/trash',
    }
    specialfolders_created = False

    def create_folder(self, folder):
        folder = folder.replace('.', '/')
        if folder in self.SPECIAL_FOLDERS_REMAP:
            folder = self.SPECIAL_FOLDERS_REMAP[folder]
            if not self.specialfolders_created:
                self.server.create_folder('specialfolders')
                self.server.create_folder('specialfolders/inbox')
                self.server.create_folder('specialfolders/mail')
                self.specialfolders_created = True
        self.server.create_folder(folder)
        return folder

    def append(self, folder, message, flags):
        self.server.append(folder, message, flags)

def to_message(message):
    message = email.message_from_string(message.encode('utf-8'))
    strio = StringIO()
    g = EmailGenerator(strio, mangle_from_=False, maxheaderlen=0)
    g.flatten(message)
    return strio.getvalue()

if __name__ == '__main__':
    main()
