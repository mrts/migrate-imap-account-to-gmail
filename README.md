migrate-imap-account-to-gmail
=============================

Python script that migrates mail from an IMAP server account to a GMail account.

Tested only with Dovecot email server, patches welcome for other servers.

Usage
-----

1. Install dependencies:

        pip install six imapclient

1. Create configuration:

        cat <<EOF > conf.py
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
        EOF

1. Run the script:

        ./migrate-imap-account-to-gmail.py
