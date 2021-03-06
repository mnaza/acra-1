# Copyright 2016, Cossack Labs Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# coding: utf-8
import socket
import json
import struct
import time
import os
import random
import string
import subprocess
import unittest
import stat
from base64 import b64decode, b64encode
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

import psycopg2
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError as SA_OperationalError
from sqlalchemy.dialects.postgresql import BYTEA

import sys
# add to path our wrapper until not published to PYPI
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'wrappers/python'))

from acrawriter import create_acrastruct


metadata = sa.MetaData()
test_table = sa.Table('test', metadata,
    sa.Column('id', sa.Integer, primary_key=True),
    sa.Column('data', sa.LargeBinary),
    sa.Column('raw_data', sa.String),
)

rollback_output_table = sa.Table('acra_rollback_output', metadata,
    sa.Column('data', sa.LargeBinary),
)

zones = []
poison_record = None
POISON_KEY_PATH = '.poison_key/poison_key'


def get_poison_record():
    """generate one poison record for speed up tests and don't create subprocess
    for new records"""
    global poison_record
    if not poison_record:
        poison_record_call = subprocess.run(
            ['./acra_genpoisonrecord'], stdout=subprocess.PIPE)
        poison_record_call.check_returncode()
        poison_record = b64decode(poison_record_call.stdout)
    return poison_record


def create_client_keypair(name, only_server=False, only_client=False):
    args = ['./acra_genkeys', '-client_id={}'.format(name)]
    if only_server:
        args.append('-acraserver')
    elif only_client:
        args.append('-acraproxy')
    return subprocess.call(args, cwd=os.getcwd(), timeout=5)


def wait_connection(port, count=10, sleep=0.1):
    """try connect to 127.0.0.1:port and close connection
    if can't then sleep on and try again (<count> times)
    if <count> times is failed than raise Exception
    """
    while count:
        try:
            connection = socket.create_connection(('127.0.0.1', port),
                                                  timeout=10)
            connection.close()
            return
        except ConnectionRefusedError:
            pass
        count -= 1
        time.sleep(sleep)
    raise Exception("can't wait connection")


def setUpModule():
    global zones
    # build binaries
    builds = [
        ['go', 'build', 'github.com/cossacklabs/acra/cmd/acraproxy'],
        ['go', 'build', 'github.com/cossacklabs/acra/cmd/acra_rollback'],
        ['go', 'build', 'github.com/cossacklabs/acra/cmd/acra_addzone'],
        ['go', 'build', 'github.com/cossacklabs/acra/cmd/acra_genpoisonrecord'],
        ['go', 'build', 'github.com/cossacklabs/acra/cmd/acra_genkeys'],
        ['go', 'build', 'github.com/cossacklabs/acra/cmd/acraserver'],
    ]
    for build in builds:
        assert subprocess.call(build, cwd=os.getcwd()) == 0

    # first keypair for using without zones
    assert create_client_keypair('keypair1') == 0
    assert create_client_keypair('keypair2') == 0
    # add two zones
    zones.append(json.loads(subprocess.check_output(
        ['./acra_addzone'], cwd=os.getcwd()).decode('utf-8')))
    zones.append(json.loads(subprocess.check_output(
        ['./acra_addzone'], cwd=os.getcwd()).decode('utf-8')))


def tearDownModule():
    import shutil
    shutil.rmtree('.acrakeys')
    for i in ['acraproxy', 'acraserver', 'acra_addzone', 'acra_genkeys',
              'acra_genpoisonrecord', 'acra_rollback']:
        try:
            os.remove(i)
        except:
            pass


class BaseTestCase(unittest.TestCase):
    DB_HOST = os.environ.get('TEST_DB_HOST', '127.0.0.1')
    DB_USER = os.environ.get('TEST_DB_USER', 'postgres')
    DB_USER_PASSWORD = os.environ.get('TEST_DB_USER_PASSWORD', 'postgres')
    DB_NAME = os.environ.get('TEST_DB_NAME', 'postgres')
    DB_PORT = os.environ.get('TEST_DB_PORT', 5432)

    PROXY_PORT_1 = 9595
    PROXY_PORT_2 = 9696
    PROXY_COMMAND_PORT_1 = 9191
    # for debugging with manually runned acra server
    EXTERNAL_ACRA = False
    ACRA_PORT = 10003
    if EXTERNAL_ACRA:
        ACRA_PORT = 9393

    ACRA_BYTEA = 'hex_bytea'
    DB_BYTEA = 'hex'
    WHOLECELL_MODE = False
    ZONE = False
    DEBUG_LOG = False
    maxDiff = None

    def fork(self, func):
        popen = func()
        count = 0
        while count <= 3:
            if popen.poll() is None:
                return popen
            count += 1
            time.sleep(0.01)
        self.fail("can't fork")

    def fork_proxy(self, proxy_port: int, acra_port: int, client_id: str, commands_port: int=None, zone_mode: bool=False, check_connection: bool=True):
        args = [
            './acraproxy', '-acra_host=127.0.0.1', '-acra_port={}'.format(acra_port),
             '-client_id={}'.format(client_id), '-port={}'.format(proxy_port),
             # now it's no matter, so just +100
            '-command_port={}'.format(commands_port if commands_port else proxy_port + 100),
             '-disable_user_check=true'
        ]
        if self.DEBUG_LOG:
            args.append('-v=true')
        if zone_mode:
            args.append('--zonemode=true')
        process = self.fork(lambda: subprocess.Popen(args))
        if check_connection:
            try:
                wait_connection(proxy_port)
            except:
                process.kill()
                raise
        return process

    def _fork_acra(self, acra_kwargs: dict, popen_kwargs: dict):
        args = {
            'db_host': self.DB_HOST,
            'db_port': self.DB_PORT,
            self.ACRA_BYTEA: 'true',
            'port': self.ACRA_PORT,
            'wholecell': 'true' if self.WHOLECELL_MODE else 'false',
            'injectedcell': 'false' if self.WHOLECELL_MODE else 'true',
            'host': '127.0.0.1',
            'd': 'true' if self.DEBUG_LOG else 'false',
            'zonemode': 'true' if self.ZONE else 'false',
            'disable_zone_api': 'false' if self.ZONE else 'true',
        }
        args.update(acra_kwargs)
        if not popen_kwargs:
            popen_kwargs = {}
        cli_args = ['--{}={}'.format(k, v) for k, v in args.items()]

        process = self.fork(lambda: subprocess.Popen(['./acraserver'] + cli_args,
                                                     **popen_kwargs))
        try:
            wait_connection(self.ACRA_PORT)
        except:
            process.kill()
            raise
        return process

    def fork_acra(self, popen_kwargs: dict=None, **acra_kwargs: dict):
        return self._fork_acra(acra_kwargs, popen_kwargs)

    def setUp(self):
        self.proxy_1 = self.fork_proxy(
            self.PROXY_PORT_1, self.ACRA_PORT, 'keypair1')
        self.proxy_2 = self.fork_proxy(
            self.PROXY_PORT_2, self.ACRA_PORT, 'keypair2')
        if not self.EXTERNAL_ACRA:
            self.acra = self.fork_acra()

        connect_args = {'user': self.DB_USER, 'password': self.DB_USER_PASSWORD}

        self.engine1 = sa.create_engine(
            'postgresql+psycopg2://127.0.0.1:{}/{}'.format(self.PROXY_PORT_1,
                self.DB_NAME), connect_args=connect_args)
        self.engine2 = sa.create_engine(
            'postgresql+psycopg2://127.0.0.1:{}/{}'.format(
                self.PROXY_PORT_2, self.DB_NAME), connect_args=connect_args)
        self.engine_raw = sa.create_engine(
            'postgresql://{}:{}/{}'.format(self.DB_HOST, self.DB_PORT, self.DB_NAME),
            connect_args=connect_args)

        self.engines = [self.engine1, self.engine2, self.engine_raw]

        metadata.create_all(self.engine_raw)
        self.engine_raw.execute('delete from test;')
        for engine in self.engines:
            count = 0
            # try with sleep if acra not up yet
            while True:
                try:
                    engine.execute(
                        "UPDATE pg_settings SET setting = '{}' "
                        "WHERE name = 'bytea_output'".format(self.DB_BYTEA))
                    break
                except Exception:
                    time.sleep(0.01)
                    count += 1
                    if count == 3:
                        raise

    def tearDown(self):
        self.proxy_1.kill()
        self.proxy_2.kill()
        processes = [self.proxy_1, self.proxy_2]
        if not self.EXTERNAL_ACRA:
            self.acra.kill()
            processes.append(self.acra)
        for p in processes:
            p.wait()
        try:
            self.engine_raw.execute('delete from test;')
        except:
            pass
        for engine in self.engines:
            engine.dispose()

    def get_random_data(self):
        size = random.randint(100, 10000)
        return ''.join(random.choice(string.ascii_letters)
                       for _ in range(size))

    def get_random_id(self):
        return random.randint(1, 100000)

    def log(self, acra_key_name, data, expected):
        """this function for printing data which used in test and for
        reproducing error with them if any error detected"""
        with open('.acrakeys/{}_zone'.format(zones[0]['id']), 'rb') as f:
            zone_private = f.read()
        with open('.acrakeys/{}'.format(acra_key_name), 'rb') as f:
            private_key = f.read()
        with open('.acrakeys/{}.pub'.format(acra_key_name), 'rb') as f:
            public_key = f.read()
        print(json.dumps(
            {
                'private_key': b64encode(private_key).decode('ascii'),
                'public_key': b64encode(public_key).decode('ascii'),
                'data': b64encode(data).decode('ascii'),
                'expected': b64encode(expected).decode('ascii'),
                'zone_private': b64encode(zone_private).decode('ascii'),
                'zone_public': zones[0]['public_key'],
                'zone_id': zones[0]['id'],
                'poison_record': b64encode(get_poison_record()).decode('ascii'),
            }
        ))


class HexFormatTest(BaseTestCase):

    def testProxyRead(self):
        """test decrypting with correct acraproxy and not decrypting with
        incorrect acraproxy or using direct connection to db"""
        keyname = 'keypair1_storage'
        with open('.acrakeys/{}.pub'.format(keyname), 'rb') as f:
            server_public1 = f.read()
        data = self.get_random_data()
        acra_struct = create_acrastruct(
            data.encode('ascii'), server_public1)
        row_id = self.get_random_id()

        self.log(keyname, acra_struct, data.encode('ascii'))

        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': acra_struct, 'raw_data': data})
        result = self.engine1.execute(
            sa.select([test_table])
            .where(test_table.c.id == row_id))
        row = result.fetchone()
        self.assertEqual(row['data'], row['raw_data'].encode('utf-8'))

        result = self.engine2.execute(
            sa.select([test_table])
            .where(test_table.c.id == row_id))
        row = result.fetchone()
        self.assertNotEqual(row['data'].decode('ascii', errors='ignore'),
                            row['raw_data'])

        result = self.engine_raw.execute(
            sa.select([test_table])
            .where(test_table.c.id == row_id))
        row = result.fetchone()
        self.assertNotEqual(row['data'].decode('ascii', errors='ignore'),
                            row['raw_data'])

    def testReadAcrastructInAcrastruct(self):
        """test correct decrypting acrastruct when acrastruct concatenated to
        partial another acrastruct"""
        keyname = 'keypair1_storage'
        with open('.acrakeys/{}.pub'.format(keyname), 'rb') as f:
            server_public1 = f.read()
        incorrect_data = self.get_random_data()
        correct_data = self.get_random_data()
        fake_offset = (3+45+84) - 4
        fake_acra_struct = create_acrastruct(
            incorrect_data.encode('ascii'), server_public1)[:fake_offset]
        inner_acra_struct = create_acrastruct(
            correct_data.encode('ascii'), server_public1)
        data = fake_acra_struct + inner_acra_struct
        row_id = self.get_random_id()

        self.log(keyname, data, fake_acra_struct+correct_data.encode('ascii'))

        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': data, 'raw_data': correct_data})
        result = self.engine1.execute(
            sa.select([test_table])
            .where(test_table.c.id == row_id))
        row = result.fetchone()
        try:
            self.assertEqual(row['data'][fake_offset:],
                             row['raw_data'].encode('utf-8'))
        except UnicodeDecodeError:
            print('incorrect data: {}\ncorrect data: {}\ndata: {}\n data len: {}'.format(
                incorrect_data, correct_data, row['data'], len(row['data'])))
            raise

        result = self.engine2.execute(
            sa.select([test_table])
            .where(test_table.c.id == row_id))
        row = result.fetchone()
        self.assertNotEqual(row['data'][fake_offset:].decode('ascii', errors='ignore'),
                            row['raw_data'])

        result = self.engine_raw.execute(
            sa.select([test_table])
            .where(test_table.c.id == row_id))
        row = result.fetchone()
        self.assertNotEqual(row['data'][fake_offset:].decode('ascii', errors='ignore'),
                            row['raw_data'])


class ZoneHexFormatTest(BaseTestCase):
    ZONE = True

    def testProxyRead(self):
        data = self.get_random_data()
        zone_public = b64decode(zones[0]['public_key'].encode('ascii'))
        acra_struct = create_acrastruct(
            data.encode('ascii'), zone_public,
            context=zones[0]['id'].encode('ascii'))
        row_id = self.get_random_id()
        self.log(zones[0]['id']+'_zone', acra_struct, data.encode('ascii'))
        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': acra_struct, 'raw_data': data})

        zone = zones[0]['id'].encode('ascii')
        result = self.engine1.execute(
            sa.select([sa.cast(zone, BYTEA), test_table])
            .where(test_table.c.id == row_id))
        row = result.fetchone()
        self.assertEqual(row['data'], row['raw_data'].encode('utf-8'))

        # without zone in another proxy, in the same proxy and without any proxy
        for engine in self.engines:
            result = engine.execute(
                sa.select([test_table])
                .where(test_table.c.id == row_id))
            row = result.fetchone()
            self.assertNotEqual(row['data'].decode('ascii', errors='ignore'),
                                row['raw_data'])

    def testReadAcrastructInAcrastruct(self):
        incorrect_data = self.get_random_data()
        correct_data = self.get_random_data()
        zone_public = b64decode(zones[0]['public_key'].encode('ascii'))
        fake_offset = (3+45+84) - 1
        fake_acra_struct = create_acrastruct(
            incorrect_data.encode('ascii'), zone_public, context=zones[0]['id'].encode('ascii'))[:fake_offset]
        inner_acra_struct = create_acrastruct(
            correct_data.encode('ascii'), zone_public, context=zones[0]['id'].encode('ascii'))
        data = fake_acra_struct + inner_acra_struct
        self.log(zones[0]['id']+'_zone', data, fake_acra_struct+correct_data.encode('ascii'))
        row_id = self.get_random_id()
        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': data, 'raw_data': correct_data})
        zone = zones[0]['id'].encode('ascii')
        result = self.engine1.execute(
            sa.select([sa.cast(zone, BYTEA), test_table])
            .where(test_table.c.id == row_id))
        row = result.fetchone()
        self.assertEqual(row['data'][fake_offset:],
                         row['raw_data'].encode('utf-8'))

        result = self.engine2.execute(
            sa.select([test_table])
            .where(test_table.c.id == row_id))
        row = result.fetchone()
        self.assertNotEqual(row['data'][fake_offset:].decode('ascii', errors='ignore'),
                            row['raw_data'])

        result = self.engine_raw.execute(
            sa.select([test_table])
            .where(test_table.c.id == row_id))
        row = result.fetchone()
        self.assertNotEqual(row['data'][fake_offset:].decode('ascii', errors='ignore'),
                            row['raw_data'])


class EscapeFormatTest(HexFormatTest):
    ACRA_BYTEA = 'escape_bytea'
    DB_BYTEA = 'escape'


class ZoneEscapeFormatTest(ZoneHexFormatTest):
    ACRA_BYTEA = 'escape_bytea'
    DB_BYTEA = 'escape'


class WholeCellMixinTest(object):
    def testReadAcrastructInAcrastruct(self):
        return


class HexFormatWholeCellTest(WholeCellMixinTest, HexFormatTest):
    WHOLECELL_MODE = True


class ZoneHexFormatWholeCellTest(WholeCellMixinTest, ZoneHexFormatTest):
    WHOLECELL_MODE = True


class EscapeFormatWholeCellTest(WholeCellMixinTest, EscapeFormatTest):
    WHOLECELL_MODE = True


class ZoneEscapeFormatWholeCellTest(WholeCellMixinTest, ZoneEscapeFormatTest):
    WHOLECELL_MODE = True


class TestConnectionClosing(BaseTestCase):
    def setUp(self):
        self.proxy_1 = self.fork_proxy(
            self.PROXY_PORT_1, self.ACRA_PORT, 'keypair1')
        if not self.EXTERNAL_ACRA:
            self.acra = self.fork_acra()

    def get_connection(self):
        return psycopg2.connect(database=self.DB_NAME, user=self.DB_USER,
                                password=self.DB_USER_PASSWORD, host=self.DB_HOST,
                                port=self.PROXY_PORT_1)

    def tearDown(self):
        self.proxy_1.kill()
        if not self.EXTERNAL_ACRA:
            self.acra.kill()
            for p in [self.proxy_1, self.acra]:
                p.wait()
        else:
            self.proxy_1.wait()

    def getActiveConnectionCount(self, cursor):
        cursor.execute('select count(*) from pg_stat_activity;')
        return int(cursor.fetchone()[0])

    def getConnectionLimit(self, connection=None):
        created_connection = False
        if connection is None:
            connection = self.get_connection()
            created_connection = True
        cursor = connection.cursor()
        cursor.execute('select setting from pg_settings where name=\'max_connections\';')
        limit = int(cursor.fetchone()[0])
        cursor.close()
        if created_connection:
            connection.close()
        return limit

    def checkConnection(self):
        """check that proxy and acra ready to accept connections"""
        count = 0
        while count <= 3:
            try:
                con = socket.create_connection(('127.0.0.1', self.ACRA_PORT), 1)
                con.close()
                con = socket.create_connection(('127.0.0.1', self.PROXY_PORT_1), 1)
                con.close()
                return
            except:
                pass
            count += 1
            time.sleep(0.1)
        self.fail("can't connect to acra or proxy")

    def testClosingConnections(self):
        self.checkConnection()
        connection = self.get_connection()

        connection.autocommit = True
        cursor = connection.cursor()
        current_connection_count = self.getActiveConnectionCount(cursor)

        connection2 = self.get_connection()
        self.assertEqual(self.getActiveConnectionCount(cursor),
                         current_connection_count+1)
        connection_limit = self.getConnectionLimit(connection)
        connections = [connection2]
        with self.assertRaises(psycopg2.OperationalError) as context_manager:
            for i in range(connection_limit):
                connections.append(self.get_connection())
        exception = context_manager.exception
        self.assertEqual(exception.args[0], 'FATAL:  sorry, too many clients already\n')

        for conn in connections:
            conn.close()
        # some wait for closing
        time.sleep(0.5)

        self.assertEqual(self.getActiveConnectionCount(cursor),
                         current_connection_count)

        # try create new connection
        connection2 = self.get_connection()
        self.assertEqual(self.getActiveConnectionCount(cursor),
                         current_connection_count + 1)

        connection2.close()
        self.assertEqual(self.getActiveConnectionCount(cursor),
                         current_connection_count)
        cursor.close()
        connection.close()


class TestKeyNonExistence(BaseTestCase):
    # 0.05 empirical selected
    PROXY_STARTUP_DELAY = 0.05

    def setUp(self):
        if not self.EXTERNAL_ACRA:
            self.acra = self.fork_acra()
        self.dsn = 'postgresql://127.0.0.1:{}@{}:{}'.format(
            self.DB_USER, self.DB_USER_PASSWORD, self.PROXY_PORT_1)

    def tearDown(self):
        if not self.EXTERNAL_ACRA:
            self.acra.kill()
            self.acra.wait()

    def delete_key(self, filename):
        os.remove('.acrakeys{sep}{name}'.format(sep=os.path.sep, name=filename))

    def test_without_acraproxy_public(self):
        """acraserver without acraproxy public key should drop connection
        from acraproxy than acraproxy should drop connection from psycopg2"""
        keyname = 'without_acraproxy_public_test'
        result = create_client_keypair(keyname)
        if result != 0:
            self.fail("Can't create keypairs")
        self.delete_key(keyname + '.pub')
        connection = None
        try:
            self.proxy = self.fork_proxy(
                self.PROXY_PORT_1, self.ACRA_PORT, keyname)
            self.assertIsNone(self.proxy.poll())
            with self.assertRaises(psycopg2.OperationalError) as exc:
                connection = psycopg2.connect(self.dsn)

        finally:
            self.proxy.kill()
            self.proxy.wait()
            if connection:
                connection.close()

    def test_without_acraproxy_private(self):
        """acraproxy shouldn't start without private key"""
        keyname = 'without_acraproxy_private_test'
        result = create_client_keypair(keyname)
        if result != 0:
            self.fail("Can't create keypairs")
        self.delete_key(keyname)
        try:
            self.proxy = self.fork_proxy(
                self.PROXY_PORT_1, self.ACRA_PORT, keyname,
                check_connection=False)
            # time for start up proxy and validation file existence.
            time.sleep(self.PROXY_STARTUP_DELAY)
            self.assertEqual(self.proxy.poll(), 1)
        finally:
            self.proxy.kill()
            self.proxy.wait()

    def test_without_acraserver_private(self):
        """acraserver without private key should drop connection
        from acraproxy than acraproxy should drop connection from psycopg2"""
        keyname = 'without_acraserver_private_test'
        result = create_client_keypair(keyname)
        if result != 0:
            self.fail("Can't create keypairs")
        self.delete_key(keyname + '_storage')
        connection = None
        try:
            self.proxy = self.fork_proxy(
                self.PROXY_PORT_1, self.ACRA_PORT, keyname)
            self.assertIsNone(self.proxy.poll())
            with self.assertRaises(psycopg2.OperationalError):
                connection = psycopg2.connect(self.dsn)
        finally:
            self.proxy.kill()
            self.proxy.wait()
            if connection:
                connection.close()

    def test_without_acraserver_public(self):
        """acraproxy shouldn't start without acraserver public key"""
        keyname = 'without_acraserver_public_test'
        result = create_client_keypair(keyname)
        if result != 0:
            self.fail("Can't create keypairs")
        self.delete_key(keyname + '_server.pub')
        try:
            self.proxy = self.fork_proxy(
                self.PROXY_PORT_1, self.ACRA_PORT, keyname,
                check_connection=False)
            # time for start up proxy and validation file existence.
            time.sleep(self.PROXY_STARTUP_DELAY)
            self.assertEqual(self.proxy.poll(), 1)
        finally:
            self.proxy.kill()
            self.proxy.wait()


class BasePoisonRecordTest(BaseTestCase):
    SHUTDOWN = True

    def setUp(self):
        super(BasePoisonRecordTest, self).setUp()
        self.log(POISON_KEY_PATH, get_poison_record(),
                 b'no matter because poison record')

    def fork_acra(self, popen_kwargs: dict=None, **acra_kwargs: dict):
        args = {
            'poisonshutdown': 'true' if self.SHUTDOWN else 'false',
        }

        if hasattr(self, 'poisonscript'):
            args['poisonscript'] = self.poisonscript

        return super(BasePoisonRecordTest, self).fork_acra(popen_kwargs, **args)


class TestPoisonRecordShutdown(BasePoisonRecordTest):
    SHUTDOWN = True

    def testShutdown(self):
        row_id = self.get_random_id()
        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': get_poison_record(), 'raw_data': 'poison_record'})
        with self.assertRaises(SA_OperationalError):
            self.engine1.execute(
                sa.select([test_table])
                .where(test_table.c.id == row_id))

    def testShutdown2(self):
        """check working poison record callback on full select"""
        row_id = self.get_random_id()
        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': get_poison_record(), 'raw_data': 'poison_record'})
        with self.assertRaises(SA_OperationalError):
            self.engine1.execute(
                sa.select([test_table]))

    def testShutdown3(self):
        """check working poison record callback on full select inside another data"""
        row_id = self.get_random_id()
        data = os.urandom(100) + get_poison_record() + os.urandom(100)
        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': data, 'raw_data': 'poison_record'})
        with self.assertRaises(SA_OperationalError):
            self.engine1.execute(
                sa.select([test_table]))


class TestShutdownPoisonRecordWithZone(TestPoisonRecordShutdown):
    ZONE = True
    WHOLECELL_MODE = False
    SHUTDOWN = True

    def testShutdown(self):
        """check callback with select by id and zone"""
        row_id = self.get_random_id()
        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': get_poison_record(), 'raw_data': 'poison_record'})
        with self.assertRaises(SA_OperationalError):
            zone = zones[0]['id'].encode('ascii')
            self.engine1.execute(
                sa.select([sa.cast(zone, BYTEA), test_table])
                    .where(test_table.c.id == row_id))

    def testShutdown2(self):
        """check callback with select by id and without zone"""
        row_id = self.get_random_id()
        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': get_poison_record(), 'raw_data': 'poison_record'})
        with self.assertRaises(SA_OperationalError):
            self.engine1.execute(
                sa.select([test_table])
                    .where(test_table.c.id == row_id))

    def testShutdown3(self):
        """check working poison record callback on full select"""
        row_id = self.get_random_id()
        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': get_poison_record(), 'raw_data': 'poison_record'})
        with self.assertRaises(SA_OperationalError):
            self.engine1.execute(
                sa.select([test_table]))

    def testShutdown4(self):
        """check working poison record callback on full select inside another data"""
        row_id = self.get_random_id()
        data = os.urandom(100) + get_poison_record() + os.urandom(100)
        self.log(POISON_KEY_PATH, data, data)
        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': data, 'raw_data': 'poison_record'})

        with self.assertRaises(SA_OperationalError):
            result = self.engine1.execute(
                sa.select([test_table]))
            # here shouldn't execute code and it's debug info
            print(result.fetchall())


class TestPoisonRecordWholeCell(TestPoisonRecordShutdown):
    WHOLECELL_MODE = True
    SHUTDOWN = True

    def testShutdown3(self):
        return


class TestShutdownPoisonRecordWithZoneWholeCell(TestShutdownPoisonRecordWithZone):
    WHOLECELL_MODE = True
    SHUTDOWN = True

    def testShutdown4(self):
        return


class AcraCatchLogsMixin(object):
    def fork_acra(self, popen_kwargs: dict=None, **acra_kwargs: dict):
        popen_args = {
            'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT,
            'close_fds': True
        }
        return super(AcraCatchLogsMixin, self).fork_acra(
            popen_args, **acra_kwargs
        )


class TestNoCheckPoisonRecord(AcraCatchLogsMixin, BasePoisonRecordTest):
    WHOLECELL_MODE = False
    SHUTDOWN = False
    DEBUG_LOG = True

    def testNoDetect(self):
        row_id = self.get_random_id()
        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': get_poison_record(), 'raw_data': 'poison_record'})
        result = self.engine1.execute(test_table.select())
        result.fetchall()
        # super() tearDown without killink acra
        super(TestNoCheckPoisonRecord, self).tearDown()

        try:
            out, er_ = self.acra.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        self.assertNotIn(b'Debug: check poison records', out)

    def tearDown(self):
        """override because it's called in single test method"""
        pass


class TestNoCheckPoisonRecordWithZone(TestNoCheckPoisonRecord):
    ZONE = True


class TestNoCheckPoisonRecordWholeCell(TestNoCheckPoisonRecord):
    WHOLECELL_MODE = True


class TestNoCheckPoisonRecordWithZoneWholeCell(TestNoCheckPoisonRecordWithZone):
    WHOLECELL_MODE = True


class TestCheckLogPoisonRecord(AcraCatchLogsMixin, BasePoisonRecordTest):
    SHUTDOWN = True
    DEBUG_LOG = True

    def setUp(self):
        self.poison_script_file = NamedTemporaryFile('w')
        # u+rwx
        os.chmod(self.poison_script_file.name, stat.S_IRWXU)
        self.poison_script = self.poison_script_file.name
        super(TestCheckLogPoisonRecord, self).setUp()

    def tearDown(self):
        self.poison_script_file.close()

    def testDetect(self):
        row_id = self.get_random_id()
        self.engine1.execute(
            test_table.insert(),
            {'id': row_id, 'data': get_poison_record(), 'raw_data': 'poison_record'})

        with self.assertRaises(SA_OperationalError):
            self.engine1.execute(test_table.select())

        # super() tearDown without killink acra
        super(TestCheckLogPoisonRecord, self).tearDown()

        try:
            out, _ = self.acra.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        self.assertIn(b'Debug: check poison records', out)


class TestKeyStorageClearing(BaseTestCase):
    def setUp(self):
        self.key_name = 'clearing_keypair'
        create_client_keypair(self.key_name)
        self.proxy_1 = self.fork_proxy(
            self.PROXY_PORT_1, self.ACRA_PORT, self.key_name, self.PROXY_COMMAND_PORT_1,
            zone_mode=True)
        if not self.EXTERNAL_ACRA:
            self.acra = self.fork_acra(
                zonemode='true', disable_zone_api='false')

        connect_args = {'user': self.DB_USER, 'password': self.DB_USER_PASSWORD}

        self.engine1 = sa.create_engine(
            'postgresql://127.0.0.1:{}/{}'.format(self.PROXY_PORT_1, self.DB_NAME),
            connect_args=connect_args)

        self.engine_raw = sa.create_engine(
            'postgresql://{}:{}/{}'.format(self.DB_HOST, self.DB_PORT, self.DB_NAME),
            connect_args=connect_args)

        self.engines = [self.engine1, self.engine_raw]

        metadata.create_all(self.engine_raw)
        self.engine_raw.execute('delete from test;')

    def tearDown(self):
        self.proxy_1.kill()
        processes = [self.proxy_1]
        if not self.EXTERNAL_ACRA:
            self.acra.kill()
            processes.append(self.acra)

        for p in processes:
            p.wait()

        try:
            self.engine_raw.execute('delete from test;')
        except:
            pass

        for engine in self.engines:
            engine.dispose()

    def test_clearing(self):
        # execute any query for loading key by acra
        result = self.engine1.execute(sa.select([1]).limit(1))
        result.fetchone()
        with urlopen('http://127.0.0.1:{}/resetKeyStorage'.format(self.PROXY_COMMAND_PORT_1)) as response:
            self.assertEqual(response.status, 200)
        # delete key for excluding reloading from FS
        os.remove('.acrakeys/{}.pub'.format(self.key_name))
        # acraserver should close connection when doesn't find key
        with self.assertRaises(SA_OperationalError):
            result = self.engine1.execute(test_table.select().limit(1))


class TestAcraRollback(BaseTestCase):
    DATA_COUNT = 5

    def setUp(self):
        connect_args = {'user': self.DB_USER, 'password': self.DB_USER_PASSWORD}
        self.engine_raw = sa.create_engine(
            'postgresql://{}:{}/{}'.format(self.DB_HOST, self.DB_PORT,
                                           self.DB_NAME),
            connect_args=connect_args)

        self.output_filename = 'acra_rollback_output.txt'
        rollback_output_table.create(self.engine_raw, checkfirst=True)

    def tearDown(self):
        try:
            self.engine_raw.execute(rollback_output_table.delete())
            self.engine_raw.execute(test_table.delete())
        except Exception as exc:
            print(exc)
        self.engine_raw.dispose()
        if os.path.exists(self.output_filename):
            os.remove(self.output_filename)

    def test_without_zone_to_file(self):
        keyname = 'keypair1_storage'
        with open('.acrakeys/{}.pub'.format(keyname), 'rb') as f:
            server_public1 = f.read()

        rows = []
        for _ in range(self.DATA_COUNT):
            data = self.get_random_data()
            row = {
                'raw_data': data,
                'data': create_acrastruct(data.encode('ascii'), server_public1),
                'id': self.get_random_id()
            }
            rows.append(row)
        self.engine_raw.execute(test_table.insert(), rows)

        subprocess.check_call(
            ['./acra_rollback', '--client_id=keypair1',
             '--connection_string=dbname={dbname} user={user} '
             'password={password} host={host} port={port}'.format(
                 dbname=self.DB_NAME, user=self.DB_USER, port=self.DB_PORT,
                 password=self.DB_USER_PASSWORD, host=self.DB_HOST),
             '--output_file={}'.format(self.output_filename),
             '--select=select data from {};'.format(test_table.name),
             '--insert=insert into {} values($1);'.format(
                 rollback_output_table.name)],
            cwd=os.getcwd())

        # execute file
        with open(self.output_filename, 'r') as f:
            for line in f:
                self.engine_raw.execute(line)

        source_data = set([i['raw_data'].encode('ascii') for i in rows])
        result = self.engine_raw.execute(rollback_output_table.select())
        result = result.fetchall()
        for data in result:
            self.assertIn(data[0], source_data)

    def test_with_zone_to_file(self):
        zone_public = b64decode(zones[0]['public_key'].encode('ascii'))
        rows = []
        for _ in range(self.DATA_COUNT):
            data = self.get_random_data()
            row = {
                'raw_data': data,
                'data': create_acrastruct(
                    data.encode('ascii'), zone_public,
                    context=zones[0]['id'].encode('ascii')),
                'id': self.get_random_id()
            }
            rows.append(row)
        self.engine_raw.execute(test_table.insert(), rows)

        subprocess.check_call(
            ['./acra_rollback', '--client_id=keypair1',
             '--connection_string=dbname={dbname} user={user} '
             'password={password} host={host} port={port}'.format(
                 dbname=self.DB_NAME, user=self.DB_USER, port=self.DB_PORT,
                 password=self.DB_USER_PASSWORD, host=self.DB_HOST),
             '--output_file={}'.format(self.output_filename),
             '--select=select \'{id}\'::bytea, data from {table};'.format(
                 id=zones[0]['id'], table=test_table.name),
             '--zonemode=true',
             '--insert=insert into {} values($1);'.format(
                 rollback_output_table.name)],
            cwd=os.getcwd())

        # execute file
        with open(self.output_filename, 'r') as f:
            for line in f:
                self.engine_raw.execute(line)

        source_data = set([i['raw_data'].encode('ascii') for i in rows])
        result = self.engine_raw.execute(rollback_output_table.select())
        result = result.fetchall()
        for data in result:
            self.assertIn(data[0], source_data)

    def test_without_zone_execute(self):
        keyname = 'keypair1_storage'
        with open('.acrakeys/{}.pub'.format(keyname), 'rb') as f:
            server_public1 = f.read()

        rows = []
        for _ in range(self.DATA_COUNT):
            data = self.get_random_data()
            row = {
                'raw_data': data,
                'data': create_acrastruct(data.encode('ascii'), server_public1),
                'id': self.get_random_id()
            }
            rows.append(row)
        self.engine_raw.execute(test_table.insert(), rows)

        subprocess.check_call(
            ['./acra_rollback', '--client_id=keypair1',
             '--connection_string=dbname={dbname} user={user} '
             'password={password} host={host} port={port}'.format(
                 dbname=self.DB_NAME, user=self.DB_USER, port=self.DB_PORT,
                 password=self.DB_USER_PASSWORD, host=self.DB_HOST),
             '--execute=true',
             '--select=select data from {};'.format(test_table.name),
             '--insert=insert into {} values($1);'.format(
                 rollback_output_table.name)],
            cwd=os.getcwd())

        source_data = set([i['raw_data'].encode('ascii') for i in rows])
        result = self.engine_raw.execute(rollback_output_table.select())
        result = result.fetchall()
        for data in result:
            self.assertIn(data[0], source_data)

    def test_with_zone_execute(self):
        zone_public = b64decode(zones[0]['public_key'].encode('ascii'))
        rows = []
        for _ in range(self.DATA_COUNT):
            data = self.get_random_data()
            row = {
                'raw_data': data,
                'data': create_acrastruct(
                    data.encode('ascii'), zone_public,
                    context=zones[0]['id'].encode('ascii')),
                'id': self.get_random_id()
            }
            rows.append(row)
        self.engine_raw.execute(test_table.insert(), rows)

        subprocess.check_call(
            ['./acra_rollback', '--client_id=keypair1',
             '--connection_string=dbname={dbname} user={user} '
             'password={password} host={host} port={port}'.format(
                 dbname=self.DB_NAME, user=self.DB_USER, port=self.DB_PORT,
                 password=self.DB_USER_PASSWORD, host=self.DB_HOST),
             '--execute=true',
             '--select=select \'{id}\'::bytea, data from {table};'.format(
                 id=zones[0]['id'], table=test_table.name),
             '--zonemode=true',
             '--insert=insert into {} values($1);'.format(
                 rollback_output_table.name)],
            cwd=os.getcwd())

        source_data = set([i['raw_data'].encode('ascii') for i in rows])
        result = self.engine_raw.execute(rollback_output_table.select())
        result = result.fetchall()
        for data in result:
            self.assertIn(data[0], source_data)


class TestAcraGenKeys(unittest.TestCase):
    def test_only_alpha_client_id(self):
        # call with directory separator in key name
        self.assertEqual(create_client_keypair(POISON_KEY_PATH), 1)


class TestIncorrectConnectRequest(BaseTestCase):
    def setUp(self):
        if not self.EXTERNAL_ACRA:
            self.acra = self.fork_acra()
            time.sleep(1)

    def tearDown(self):
        if not self.EXTERNAL_ACRA:
            self.acra.kill()
            self.acra.wait()

    def test_big_data_block(self):
        check_size = (8 * 1024) + 1  # 8 kb + 1 extra byte
        # check that acra will close connection if send block size > 8kb
        with socket.create_connection(('127.0.0.1', self.ACRA_PORT), 10) as connection:
            big_size = struct.pack('<I', check_size)
            connection.send(big_size)
            # time for closing socket by acraserver and system
            time.sleep(0.5)
            with self.assertRaises(BrokenPipeError):
                # here should be closed connection from acraserver side
                # first send to try send and get closed flag (but no any raises)
                connection.send(b'1')
                # second send to get exception
                connection.send(b'1')


if __name__ == '__main__':
    unittest.main()
