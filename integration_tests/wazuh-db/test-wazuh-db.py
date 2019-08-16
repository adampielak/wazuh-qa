#! /usr/bin/python3
# July 16, 2019

from socket import socket, AF_UNIX, SOCK_STREAM
from struct import pack, unpack
from random import choice, randint, randrange
from string import ascii_lowercase
from os import listdir, unlink
import json
from testsuite import *

def random_string(length=32):
    return ''.join([choice(ascii_lowercase) for i in range(length)])


def random_fim():
    block0 = randrange(0, 100000)
    block1 = block0 // 2209
    block2 = block1 // 47

    return {
        'file': random_string(),
        'type': choice(('file', 'registry')),
        'size': randint(0, 1000000000),
        'perm': randint(0, 0xFFFFFFFF),
        'uid': random_string(8),
        'gid': random_string(8),
        'md5': random_string(8),
        'sha1': random_string(8),
        'uname': random_string(8),
        'gname': random_string(8),
        'mtime': randint(0, 1000000000),
        'inode': randint(0, 1000000000),
        'sha256': random_string(8),
        'attrs': randint(0, 1000000000),
        'symbolic_path': random_string(8),
        'checksum': random_string(16),
        'blocks': (block0, block1, block2)
    }

class Database:
    def __init__(self, id=0):
        self.connect()
        self.set_id(id)

    def __enter__(self):
        return self

    def __exit__(self, xc_type, exc_value, traceback):
        self.sock.close()

    def set_id(self, id):
        self.id = str(id)

    def connect(self):
        self.sock = socket(AF_UNIX, SOCK_STREAM)
        self.sock.connect("{0}/queue/db/wdb".format(get_directory()))

    def send(self, msg):
        msg = msg.encode()
        return self.sock.send(pack("<I{0}s".format(len(msg)), len(msg), msg))

    def recv(self):
        length = self.sock.recv(4)
        return self.sock.recv(unpack("<I", length)[0]).decode()

    def load(self, path):
        self.send("agent {0} syscheck load {1}".format(self.id, path))
        return self.recv()

    def save(self, path, checksum):
        self.send("agent {0} syscheck save file {1} {2}".format(self.id, checksum, path))
        return self.recv()

    def save2(self, data):
        self.send("agent {0} syscheck save2 {1}".format(self.id, json.dumps(data)))
        return self.recv()

    def make_integrity(self):
        self.send("agent {0} syscheck make_integrity ".format(self.id))
        return self.recv()

    def remove(self):
        self.send("agent {0} remove".format(self.id))
        return self.recv()

    def remove_multiple(self, agents):
        self.send("wazuhdb remove {0}".format(' '.join(agents)))
        return self.recv()


def test_connect():
    '''Test connection to Wazuh DB'''

    try:
        with Database() as db:
            pass
    except Exception as e:
        print('# {0}'.format(e))
        return False

    return True

def test_fim(databases, queries):
    '''Connect to Wazuh DB and run FIM queries'''

    try:
        with Database('000') as db:
            for i in range(1, databases + 1):
                db.set_id(i)

                for _ in range(queries):
                    path = random_string()
                    ans = db.load(path)

                    if not ans.startswith("ok"):
                        raise Exception("Cannot load {0}: {1}".format(path, ans))

                    ans = db.save(path, "0:0:0:0:0:0:0:0:0:0:0:0:0!0:0")

                    if ans != "ok":
                        raise Exception("Cannot save {0}: {1}".format(path, ans))

    except Exception as e:
        print('# {0}'.format(e))
        return False

    return True


def test_fim_insert(queries):
    '''Insert files into agent 001 database'''

    try:
        with Database('001') as db:
            for _ in range(1, queries):
                fim = random_fim()
                ans = db.save2(fim)

                if ans != "ok":
                    raise Exception("Cannot save {0}: {1}".format(fim['file'], ans))

    except Exception as e:
        print('# {0}'.format(e))
        return False

    return True


def test_fim_make_integrity():
    '''Make a FIM database integrity blockset'''

    try:
        with Database('001') as db:
            ans = db.make_integrity()

            if not ans.startswith("ok"):
                raise Exception("Cannot make integrity: {0}".format(ans))

    except Exception as e:
        print('# {0}'.format(e))
        return False

    return True


def test_remove_individual(n):
    '''Remove database files'''

    try:
        with Database() as db:
            for i in range(1, n + 1):
                db.set_id(i)
                ans = db.remove()

                if ans != ("ok"):
                    raise Exception("Cannot remove {0}: {1}".format(i, ans))

    except Exception as e:
        print('# {0}'.format(e))
        return False

    return True

def test_remove_multiple(n):
    '''Remove multiple databases'''

    try:
        with Database() as db:
            ans = db.remove_multiple([str(i) for i in range(1, n + 1)])

            if not ans.startswith("ok "):
                raise Exception("Cannot remove: {0}".format(ans))

            json.loads(ans[3:])

    except Exception as e:
        print('# {0}'.format(e))
        return False

    return True

def teardown():
    BLACKLIST = ('.template.db', '000.db', '000.db-shm', '000.db-wal', 'wdb')
    wdb_dir = '{0}/queue/db'.format(get_directory())

    for i in listdir(wdb_dir):
        if i not in BLACKLIST:
            try:
                unlink('{0}/{1}'.format(wdb_dir, i))
            except Exception:
                pass


if __name__ == "__main__":
    test = TestSuite()

    test.append("Connect to Wazuh DB", test_connect())
    test.append("Run 50 FIM queries to 500 agents", test_fim(500, 50))
    test.append("Remove 1000 databases individually", test_remove_individual(1000))
    test.append("Remove 1000 databases", test_remove_multiple(1000))
    test.append("Remove 5000 databases", test_remove_multiple(5000), expected=False)
    test.append("Insert 1000 FIM files", test_fim_insert(1000))
    test.append("Create a FIM database integrity blockset", test_fim_make_integrity())

    print(test)
    teardown()
