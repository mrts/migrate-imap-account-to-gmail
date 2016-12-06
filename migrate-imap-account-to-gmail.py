#!/usr/bin/env python
# encoding: utf-8
"""
Script that copies mail from one IMAP account to another.

Has been used to migrate more than 10 000 messages from a Dovecot server to
GMail.

`pip install https://bitbucket.org/mrts/imapclient/get/default.zip six` and
create `conf.py` as follows to use it:

SOURCE = {
    'HOST': 'example.com',
    'USERNAME': 'user',
    'PASSWORD': 'password',
    'SSL': True,
    'IGNORE_FOLDERS': ('[Gmail]',
                       '[Gmail]/Trash', '[Gmail]/Spam',
                       '[Gmail]/Starred', '[Gmail]/Important')
}

TARGET = {
    'HOST': 'imap.gmail.com',
    'USERNAME': 'user@gmail.com',
    'PASSWORD': 'password',
    'SSL': True,
    'ROOT_FOLDER': 'example-com-archive'
}
"""

from __future__ import unicode_literals
import time
import datetime
import email
import sqlite3
import re
import codecs
import locale
import sys
from email.generator import Generator as EmailGenerator
from six.moves import input
from imapclient import IMAPClient
from optparse import OptionParser

import conf

def migrateMail(options):
    source_account = Source(conf.SOURCE)
    target_account = Target(conf.TARGET, source_account.folder_separator())

    if (options.confirm and not options.listFoldersOnly):
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

    total_sync_start = time.time()
    total_messages = 0
    total_bytes = 0

    if (options.listFoldersOnly):
        print "Source Folders:"
        for folder in source_account.list_folders():
            status = "[Migreated Directly]"
            if source_account.is_ignored(folder):
                status = "[Ignored]"
            if source_account.map_target_folder(folder):
                status = "[Migreated to: " + source_account.map_target_folder(folder) + "]"
            print "    {0:20} : {1}".format(folder, status)
        print "\nDestination Folders:"
        for folder in target_account.list_folders():
            print "    " + folder
    else:
        for folder in source_account.list_folders():
            if source_account.is_ignored(folder):
                if options.verbose: print("\t'%s' is in ignored folders, skipping" % folder)
                continue
            folder_sync_start = time.time()
            target_folder = source_account.map_target_folder(folder)
            if not target_folder:
                # Assume that there is a folder with the same name.
                target_folder = folder
            if options.verbose: print "Synchronizing folder '%s' to '%s'" % (folder, target_folder)
            target_folder = target_account.create_folder(target_folder)
            folder_info = source_account.select_folder(folder)
            if options.verbose: print("\tcontains %s messages" % folder_info['EXISTS'])
            for message_id in source_account.fetch_message_ids():
                msg, flags, size, date = source_account.fetch_message(message_id)
                if options.verbose: print("\t\tuploading message '%s' of %s bytes to '%s'" %
                        (message_id, size, target_folder))
                target_account.append(target_folder, msg, flags, date)
                db.mark_message_seen(message_id, target_folder)
                total_messages += 1
                total_bytes += size
                if options.deleteSource: source_account.delete_message(message_id)
            if options.verbose: print("\t'%s' done, took %s seconds, %d total messages uploaded" %
                    (folder, time.time() - folder_sync_start, total_messages))

            run_duration = datetime.timedelta(seconds=time.time() - total_sync_start)
            if options.verbose: print("Synchronization of %d messages (%s bytes) finished, took %s" %
                    (total_messages, total_bytes, run_duration))

    db.close()

class Base(object):
    def __init__(self, conf):
        self.username = conf['USERNAME']
        self.host = conf['HOST']
        self.server = IMAPClient(conf['HOST'], use_uid=True, ssl=conf['SSL'])
        self.server.login(conf['USERNAME'], conf['PASSWORD'])

    def __str__(self):
        return "<user: %s | host: %s>" % (self.username, self.host)

    def folder_separator(self):
        return self.server.namespace()[0][0][1]

    def list_folders(self):
        return sorted(folderinfo[2] for folderinfo in self.server.list_folders())


class Source(Base):
    def __init__(self, conf):
        super(Source, self).__init__(conf)
        self.ignore_folders = conf['IGNORE_FOLDERS']
        self.folder_mapping = conf['FOLDER_MAPPING']

    def is_ignored(self, folder):
        if folder in self.ignore_folders:
            return True

    def select_folder(self, folder):
        return self.server.select_folder(folder)

    def fetch_message_ids(self):
        return self.server.search([b'NOT', b'DELETED'])

    def fetch_message(self, message_id):
        response = self.server.fetch((message_id,),
                ['FLAGS', 'RFC822', 'RFC822.SIZE', 'INTERNALDATE'])
        if options.verbose: print str(len(response)) + " : " + str(message_id)
#        assert len(response) == 1
        data = response[message_id]
        return (data['RFC822'], data['FLAGS'],
                data['RFC822.SIZE'], data['INTERNALDATE'])

    def delete_message(self, message_id):
        response = self.server.delete_messages(message_id)
        if options.verbose: print "Deleted: " + str(message_id) + " - " + str(response)
        self.server.expunge()
        return

    def map_target_folder(self, folder):
        if folder in self.folder_mapping:
            return self.folder_mapping[folder]
        else:
            return None


class Target(Base):
    def __init__(self, conf, source_folder_separator):
        super(Target, self).__init__(conf)
        self.root_folder = conf['ROOT_FOLDER']
        self.source_folder_separator = source_folder_separator
        self.target_folder_separator = self.folder_separator()
        if not self.server.folder_exists(self.root_folder):
            self.server.create_folder(self.root_folder)

    def __str__(self):
        s = super(Target, self).__str__()
        return s.replace('>', ' | root folder: %s>' % self.root_folder)

    def create_folder(self, folder):
        if len(self.root_folder) > 0:
            if self.source_folder_separator != self.target_folder_separator:
                folder = folder.replace(self.source_folder_separator,
                        self.target_folder_separator)
            folder = self.root_folder + self.target_folder_separator + folder
        if not self.server.folder_exists(folder):
            self.server.create_folder(folder)
        return folder

    def append(self, folder, message, flags, date):
        self.server.append(folder, message, flags, date)


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

if __name__ == '__main__':
    # Wrap sys.stdout into a StreamWriter to allow writing unicode in case of
    # redirection. See http://stackoverflow.com/questions/4545661/unicodedecodeerror-when-redirecting-to-file
    sys.stdout = codecs.getwriter(locale.getpreferredencoding())(sys.stdout)

    parser = OptionParser()
    parser.add_option("-d", "--delete-source", action="store_true", dest="deleteSource", default=False, help="delete migrated messages from the source account")
    parser.add_option("-f", "--force", action="store_false", dest="confirm", default=True, help="force migration of messages without user prompt.")
    parser.add_option("-q", "--quiet", action="store_false", dest="verbose", default=True, help="do not write any processing commentary to stdout.")
    parser.add_option("-l", "--list-folders", action="store_true", dest="listFoldersOnly", default=False, help="list source and destination folders and exit.")

    (options, args) = parser.parse_args()

    migrateMail(options)
