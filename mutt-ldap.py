#!/usr/bin/env python2
#
# Copyright (C) 2008-2013  W. Trevor King
# Copyright (C) 2012-2013  Wade Berrier
# Copyright (C) 2012       Niels de Vos
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""LDAP address searches for Mutt.

Add :file:`mutt-ldap.py` to your ``PATH`` and add the following line
to your :file:`.muttrc`::

  set query_command = "mutt-ldap.py '%s'"

Search for addresses with `^t`, optionally after typing part of the
name.  Configure your connection by creating :file:`~/.mutt-ldap.rc`
contaning something like::

  [connection]
  server = myserver.example.net
  basedn = ou=people,dc=example,dc=net

See the `CONFIG` options for other available settings.
"""

import email.utils
import hashlib
import itertools
import os.path
import ConfigParser
import pickle

import ldap
import ldap.sasl


CONFIG = ConfigParser.SafeConfigParser()
CONFIG.add_section('connection')
CONFIG.set('connection', 'server', 'domaincontroller.yourdomain.com')
CONFIG.set('connection', 'port', '389')  # set to 636 for default over SSL
CONFIG.set('connection', 'ssl', 'no')
CONFIG.set('connection', 'starttls', 'no')
CONFIG.set('connection', 'basedn', 'ou=x co.,dc=example,dc=net')
CONFIG.add_section('auth')
CONFIG.set('auth', 'user', '')
CONFIG.set('auth', 'password', '')
CONFIG.set('auth', 'gssapi', 'no')
CONFIG.add_section('query')
CONFIG.set('query', 'filter', '') # only match entries according to this filter
CONFIG.set('query', 'search_fields', 'cn displayName uid mail') # fields to wildcard search
CONFIG.add_section('results')
CONFIG.set('results', 'optional_column', '') # mutt can display one optional column
CONFIG.add_section('cache')
CONFIG.set('cache', 'enable', 'yes') # enable caching by default
CONFIG.set('cache', 'path', '~/.mutt-ldap.cache') # cache results here
#CONFIG.set('cache', 'longevity_days', '14') # TODO: cache results for 14 days by default
CONFIG.add_section('system')
# HACK: Python 2.x support, see http://bugs.python.org/issue2128
CONFIG.set('system', 'argv-encoding', 'utf-8')

CONFIG.read(os.path.expanduser('~/.mutt-ldap.rc'))


class LDAPConnection (object):
    """Wrap an LDAP connection supporting the 'with' statement

    See PEP 343 for details.
    """
    def __init__(self, config=None):
        if config is None:
            config = CONFIG
        self.config = config
        self.connection = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, type, value, traceback):
        self.unbind()

    def connect(self):
        if self.connection is not None:
            raise RuntimeError('already connected to the LDAP server')
        protocol = 'ldap'
        if self.config.getboolean('connection', 'ssl'):
            protocol = 'ldaps'
        url = '{0}://{1}:{2}'.format(
            protocol,
            self.config.get('connection', 'server'),
            self.config.get('connection', 'port'))
        self.connection = ldap.initialize(url)
        if (self.config.getboolean('connection', 'starttls') and
                protocol == 'ldap'):
            self.connection.start_tls_s()
        if self.config.getboolean('auth', 'gssapi'):
            sasl = ldap.sasl.gssapi()
            self.connection.sasl_interactive_bind_s('', sasl)
        else:
            self.connection.bind(
                self.config.get('auth', 'user'),
                self.config.get('auth', 'password'),
                ldap.AUTH_SIMPLE)

    def unbind(self):
        if self.connection is None:
            raise RuntimeError('not connected to an LDAP server')
        self.connection.unbind()
        self.connection = None

    def search(self, query):
        if self.connection is None:
            raise RuntimeError('connect to the LDAP server before searching')
        post = u''
        if query:
            post = u'*'
        fields = self.config.get('query', 'search_fields').split()
        filterstr = u'(|{0})'.format(
            u' '.join([u'({0}=*{1}{2})'.format(field, query, post) for
                       field in fields]))
        query_filter = self.config.get('query', 'filter')
        if query_filter:
            filterstr = u'(&({0}){1})'.format(query_filter, filterstr)
        msg_id = self.connection.search(
            self.config.get('connection', 'basedn'),
            ldap.SCOPE_SUBTREE,
            filterstr.encode('utf-8'))
        res_type = None
        while res_type != ldap.RES_SEARCH_RESULT:
            try:
                res_type, res_data = self.connection.result(
                    msg_id, all=False, timeout=0)
            except ldap.ADMINLIMIT_EXCEEDED:
                #print "Partial results"
                break
            if res_data:
                # use `yield from res_data` in Python >= 3.3, see PEP 380
                for entry in res_data:
                    yield entry


class CachedLDAPConnection (LDAPConnection):
    def connect(self):
        self._load_cache()
        super(CachedLDAPConnection, self).connect()

    def unbind(self):
        super(CachedLDAPConnection, self).unbind()
        self._save_cache()

    def search(self, query):
        cache_hit, entries = self._cache_lookup(query=query)
        if cache_hit:
            # use `yield from res_data` in Python >= 3.3, see PEP 380
            for entry in entries:
                yield entry
        else:
            entries = []
            for entry in super(CachedLDAPConnection, self).search(query=query):
                entries.append(entry)
                yield entry
            self._cache_store(query=query, entries=entries)

    def _load_cache(self):
        path = os.path.expanduser(self.config.get('cache', 'path'))
        try:
            self._cache = pickle.load(open(path, 'rb'))
        except IOError:  # probably "No such file"
            self._cache = {}
        except (ValueError, KeyError):  # probably a corrupt cache file
            self._cache = {}

    def _save_cache(self):
        path = os.path.expanduser(self.config.get('cache', 'path'))
        pickle.dump(self._cache, open(path, 'wb'))

    def _cache_store(self, query, entries):
        self._cache[self._cache_key(query=query)] = entries

    def _cache_lookup(self, query):
        entries = self._cache.get(self._cache_key(query=query), None)
        if entries is None:
            return (False, entries)
        return (True, entries)

    def _cache_key(self, query):
        return (self._config_id(), query)

    def _config_id(self):
        """Return a unique ID representing the current configuration
        """
        config_string = pickle.dumps(self.config)
        return hashlib.sha1(config_string).hexdigest()


def format_columns(address, data):
    yield unicode(address, 'utf-8')
    yield unicode(data.get('displayName', data['cn'])[-1], 'utf-8')
    optional_column = CONFIG.get('results', 'optional_column')
    if optional_column in data:
        yield unicode(data[optional_column][-1], 'utf-8')

def format_entry(entry):
    cn,data = entry
    if 'mail' in data:
        for m in data['mail']:
            # http://www.mutt.org/doc/manual/manual-4.html#ss4.5
            # Describes the format mutt expects: address\tname
            yield u'\t'.join(format_columns(m, data))


if __name__ == '__main__':
    import sys

    # HACK: convert sys.argv to Unicode (not needed in Python 3)
    argv_encoding = CONFIG.get('system', 'argv-encoding')
    sys.argv = [unicode(arg, argv_encoding) for arg in sys.argv]

    if len(sys.argv) < 2:
        sys.stderr.write(u'{0}: no search string given\n'.format(sys.argv[0]))
        sys.exit(1)

    query = u' '.join(sys.argv[1:])

    if CONFIG.getboolean('cache', 'enable'):
        connection_class = CachedLDAPConnection
    else:
        connection_class = LDAPConnection

    addresses = []
    with connection_class() as connection:
        entries = connection.search(query=query)
        for entry in entries:
            addresses.extend(format_entry(entry))
    print(u'{0} addresses found:'.format(len(addresses)))
    print(u'\n'.join(addresses))
