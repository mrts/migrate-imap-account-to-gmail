#!/usr/bin/env python
# encoding: utf-8
"""
Script that copies mail from a Dovecot IMAP server account to a GMail account.

`pip install https://bitbucket.org/mrts/imapclient/get/default.zip six` and create `conf.py` as follows to use it:

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
import sqlite3
import re
from email.generator import Generator as EmailGenerator
from six.moves import input

from imapclient import IMAPClient

import conf

def main():
    source_account = Source(conf.SOURCE)
    target_account = Target(conf.TARGET)

    yes = input("Copy all mail\n"
            "from account\n"
            "\t%s\n"
            "to account\n"
            "\t%s\n[yes/no]? " %
            (source_account, target_account))
    if yes != "yes":
        print("Didn't enter 'yes', exiting")
        return

    db = Database()
    db.create_tables()

    for folder in source_account.list_folders():
        print("Synchronizing folder '%s'" % folder)
        start = time.time()
        target_folder = target_account.create_folder(folder)
        folder_info = source_account.select_folder(folder)
        print("\tcontains %s messages" % folder_info['EXISTS'])
        for message_id in source_account.fetch_message_ids():
            if db.is_message_seen(target_folder, message_id):
                print("\t\tskipping message '%s', already uploaded to '%s'" %
                        (message_id, target_folder))
                continue
            msg, flags, size, date = source_account.fetch_message(message_id)
            print("\t\tuploading message '%s' of %s bytes to '%s'" %
                    (message_id, size, target_folder))
            target_account.append(target_folder, msg, flags, date)
            db.mark_message_seen(target_folder, message_id)
        end = time.time()
        print("\t'%s' done, took %s seconds" % (folder, end - start))
    db.close()

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
        return sorted(folderinfo[2] for folderinfo in self.server.list_folders())

    def select_folder(self, folder):
        return self.server.select_folder(folder)

    def fetch_message_ids(self):
        return self.server.search(['NOT DELETED'])

    def fetch_message(self, message_id):
        response = self.server.fetch((message_id,),
                ['FLAGS', 'RFC822', 'RFC822.SIZE', 'INTERNALDATE'],
                do_decode=False)
        assert len(response) == 1
        data = response[message_id]
        return (data['RFC822'], data['FLAGS'],
                data['RFC822.SIZE'], data['INTERNALDATE'])

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

    def create_folder(self, folder):
        folder = folder.replace('.', '/')
        if folder in self.SPECIAL_FOLDERS_REMAP:
            folder = self.SPECIAL_FOLDERS_REMAP[folder]
            for parent in ('specialfolders',
                    'specialfolders/inbox','specialfolders/mail'):
                if not self.server.folder_exists(parent):
                    self.server.create_folder(parent)
        if not self.server.folder_exists(folder):
            self.server.create_folder(folder)
        return folder

    def append(self, folder, message, flags, date):
        self.server.append(folder, message, flags, date, do_encode=False)

class Database(object):
    def __init__(self):
        self.connection = sqlite3.connect(__file__ + ".sqlite")

    def create_tables(self):
        with self.connection:
            self.connection.execute("CREATE TABLE IF NOT EXISTS "
                    "seen_messages (folder text, msgid number)")
            self.connection.execute("CREATE INDEX IF NOT EXISTS "
                    "seen_messages_idx ON seen_messages (folder, msgid)")

    def mark_message_seen(self, message_id, target_folder):
        with self.connection:
            self.connection.execute("INSERT INTO seen_messages VALUES (?, ?)",
                    (target_folder, message_id))

    def is_message_seen(self, message_id, target_folder):
        with self.connection:
            return self.connection.execute("SELECT 1 FROM seen_messages "
                    "WHERE folder=? AND msgid=? LIMIT 1",
                    (target_folder, message_id)).fetchone()

    def close(self):
        self.connection.close()

# Wrap sys.stdout into a StreamWriter to allow writing unicode in case of
# redirection.
# See http://stackoverflow.com/questions/4545661/unicodedecodeerror-when-redirecting-to-file
import codecs
import locale
import sys

sys.stdout = codecs.getwriter(locale.getpreferredencoding())(sys.stdout)

if __name__ == '__main__':
    main()
