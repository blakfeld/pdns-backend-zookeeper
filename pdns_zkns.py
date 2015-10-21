#!/usr/bin/env python
"""PowerDNS remote http backend for Zookeeper/finagle serversets.

Not yet implemented:
    - NS records
    - SRV records (with port numbers, etc)
    - Instrumentation (Prometheus metrics)
    - Some kind of status page
    - Correct handling of ANY queries
"""

import time

from pyglib import app
from pyglib import flags
from pyglib import log
from twitter.common import http
from twitter.common.exceptions import ExceptionalThread
from twitter.common.http.diagnostics import DiagnosticsEndpoints
from twitter.common.zookeeper import kazoo_client
from twitter.common.zookeeper.serverset import serverset

FLAGS = flags.FLAGS


flags.DEFINE_string('zk', 'localhost:2181/',
                    'Zookeeper ensemble (comma-delimited, optionally '
                    'followed by /chroot path)')
flags.DEFINE_string('domain', 'zk.example.com',
                    'Serve records for this DNS domain.')
flags.DEFINE_integer('port', 8080, 'HTTP listen port.')
flags.DEFINE_string('listen', '0.0.0.0',
                    'IP address to listen for http connections.')

flags.DEFINE_integer('ttl', 60, 'TTL for normal records.')
flags.DEFINE_integer('soa_ttl', 300, 'TTL for SOA record itself.')
flags.DEFINE_string('soa_nameserver', '',
                    'Authoritative nameserver for the SOA record. '
                    'Autogenerated if left blank.')
flags.DEFINE_string('soa_email', '',
                    'Email address field for the SOA record. '
                    'Autogenerated if left blank.')
flags.DEFINE_integer('soa_refresh', 1200,
                     'Refresh field for the SOA record.')
flags.DEFINE_integer('soa_retry', 180,
                     'Retry field for the SOA record.')
flags.DEFINE_integer('soa_expire', 86400,
                     'Expire field for the SOA record.')
flags.DEFINE_integer('soa_nxdomain_ttl', 60,
                     'Negative caching TTL for the SOA record.')


class SOAData(object):
    """DNS SOA data representation."""

    def __init__(self, ttl, ns, email, refresh, retry, expire, nxdomain_ttl):
        self.ttl = int(ttl)
        self.ns = str(ns)
        self.email = str(email)
        self.refresh = int(refresh)
        self.retry = int(retry)
        self.expire = int(expire)
        self.nxdomain_ttl = int(nxdomain_ttl)

    def __str__(self):
        # Can't decide if this is cool or a terrible hack.
        return ('%(ns)s %(email)s %(refresh)s 1 '
                '%(retry)s %(expire)s %(nxdomain_ttl)s') % self.__dict__


def dnsresponse(data):
    """Construct a response for the PowerDNS remote backend.

    Remote api docs:
        https://doc.powerdns.com/md/authoritative/backend-remote/
    """
    resp = {'result': data}
    log.debug('DNS response: %s', resp)
    return resp


class ZknsServer(http.HttpServer, DiagnosticsEndpoints):
    """Zookeeper-backed powerdns remote api backend"""

    def __init__(self, zk_handle, domain, ttl, soa_data):
        self.zkclient = zk_handle
        self.domain = domain.strip('.')
        self.soa_data = soa_data
        self.ttl = ttl

        DiagnosticsEndpoints.__init__(self)
        http.HttpServer.__init__(self)

    def a_response(self, qname):
        """Generate a pdns A query response."""
        instances = self.resolve_hostname(qname)
        return dnsresponse([
            {'qtype': 'A',
             'qname': qname,
             'ttl': self.ttl,
             'content': x.service_endpoint.host} for x in instances
            ])

    def soa_response(self, qname):
        """Generate a pdns SOA query response."""
        if not qname.lower().strip('.').endswith(self.domain):
            return dnsresponse(False)

        return dnsresponse([
            {'qtype': 'SOA',
             'qname': self.domain,
             'ttl': self.soa_data.ttl,
             'content': str(self.soa_data)}
            ])

    @http.route('/dnsapi/lookup/<qname>/<qtype>', method='GET')
    def dnsapi_lookup(self, qname, qtype):
        """pdns lookup api"""
        log.debug('QUERY: %s %s', qname, qtype)
        # TODO: better ANY handling (what's even correct here?)
        if qtype in ['A', 'ANY']:
            return self.a_response(qname)
        elif qtype == 'SOA':
            return self.soa_response(qname)
        else:
            return dnsresponse(False)

    @staticmethod
    @http.route('/dnsapi/getDomainMetadata/<qname>/<qkind>', method='GET')
    def dnsapi_getdomainmetadata(qname, qkind):
        """pdns getDomainMetadata api"""
        log.debug('QUERY: %s %s', qname, qkind)
        if qkind == 'SOA-EDIT':
            # http://jpmens.net/2013/01/18/understanding-powerdns-soa-edit/
            return dnsresponse(['EPOCH'])
        else:
            return dnsresponse(False)

    def resolve_hostname(self, hostname):
        """Resolve a hostname to a list of serverset instances."""
        zkpaths = construct_paths(hostname, self.domain)
        while True:
            try:
                zkpath, shard = zkpaths.next()
                sset = list(serverset.ServerSet(self.zkclient, zkpath))
                if not sset:
                    continue
                elif shard is None:
                    return sset
                else:
                    for ss_instance in sset:
                        if ss_instance.shard == shard:
                            return [ss_instance]
                    continue
            except StopIteration:
                log.info('nothing found')
                return []


def construct_paths(hostname, basedomain=None):
    """Generate paths to search for a serverset in Zookeeper.

    Yields tuples of (<subpath to search>, <shard number or None>).

    >>> construct_paths('0.job.foo.bar.bas.buz.basedomain.example.com',
                        'basedomain.example.com')
    ('buz/bas/bar/foo/job', 0)
    ('buz/bas/bar/job.foo', 0)
    ('buz/bas/job.foo.bar', 0)
    ('buz/job.foo.bar.bas', 0)
    ('job.foo.bar.bas.buz', 0)
    """
    # e.g. 0.job.foo.bar.bas.buz.basedomain.example.com
    if basedomain:
        qrec, _, _ = hostname.strip('.').rpartition(basedomain)
    else:
        qrec = hostname.strip('.')
    # -> 0.job.foo.bar.bas.buz

    path_components = list(reversed(qrec.strip('.').split('.')))
    # -> ['buz', 'bas', 'bar', 'foo', 'job', '0']

    # maybe it has a shard number?
    try:
        shard = int(path_components[-1])
        path_components = path_components[:-1]
    except ValueError:
        shard = None

    while path_components:
        yield ('/'.join(path_components), shard)
        if len(path_components) == 1:
            return

        # Extend the last element with the previous one
        # e.g. ['a', 'b', 'c', 'f.e.d'] --> ['a', 'b', 'f.e.d.c']
        elem = '.'.join([path_components.pop(), path_components.pop()])
        path_components.append(elem)


def wait_forever():
    """An interruptable do-nothing-forever sleep."""
    while True:
        time.sleep(60)


def main(_):
    """Main"""
    zkconn = kazoo_client.TwitterKazooClient(FLAGS.zk)
    zkconn.start()

    soa_data = SOAData(ttl=FLAGS.soa_ttl,
                       ns=FLAGS.soa_nameserver or 'ns1.%s' % FLAGS.domain,
                       email=FLAGS.soa_email or 'root.%s' % FLAGS.domain,
                       refresh=FLAGS.soa_refresh,
                       retry=FLAGS.soa_retry,
                       expire=FLAGS.soa_expire,
                       nxdomain_ttl=FLAGS.soa_nxdomain_ttl)

    server = ZknsServer(zk_handle=zkconn,
                        domain=FLAGS.domain,
                        ttl=FLAGS.ttl,
                        soa_data=soa_data)

    thread = ExceptionalThread(
        target=lambda: server.run(FLAGS.listen,
                                  FLAGS.port,
                                  server='cherrypy'))
    thread.daemon = True
    thread.start()

    try:
        wait_forever()
    except KeyboardInterrupt:
        log.fatal('KeyboardInterrupt! Shutting down.')


if __name__ == '__main__':
    app.run()
