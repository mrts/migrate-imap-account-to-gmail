migrate-imap-account-to-gmail
=============================

Python script that migrates mail from an IMAP server account to a GMail
account. Preserves source account folder structure and saves mail under a
configurable root folder in target account (set `TARGET['ROOT_FOLDER']`).
Folders can be skipped by listing them in `SOURCE['IGNORE_FOLDERS']`.
Tracks migration in database so that migration will continue from the last seen
message in case of interruption or when new mail needs to be synchronized from
the source account.

Tested with Dovecot to GMail and GMail to GMail email migration.
Should also work with a non-GMail target account.

Usage
-----

1. Install dependencies:

        pip install six https://bitbucket.org/mrts/imapclient/get/default.zip

1. Create configuration:

        cat <<EOF > conf.py
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
        EOF

1. Run the script:

        ./migrate-imap-account-to-gmail.py

It may take a while, here's sample output from a live run:

    Synchronization of 12571 messages finished, took 6:44:35.101650
