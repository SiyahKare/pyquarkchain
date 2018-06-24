import copy
import sys
import gevent
import asyncio
import ipaddress
import socket
import random

from devp2p import peermanager
from devp2p.app import BaseApp
from devp2p.discovery import NodeDiscovery
from devp2p.protocol import BaseProtocol
from devp2p.service import BaseService, WiredService
from devp2p.crypto import privtopub as privtopub_raw, sha3
from devp2p.utils import host_port_pubkey_to_uri, update_config_with_defaults
from rlp.utils import decode_hex, encode_hex

from quarkchain.core import random_bytes
from quarkchain.cluster.protocol import (
    P2PConnection, ROOT_SHARD_ID,
)
from quarkchain.cluster.p2p_commands import CommandOp
from quarkchain.cluster.p2p_commands import GetPeerListRequest
from quarkchain.utils import Logger
from quarkchain.cluster.simple_network import Peer

try:
    import ethereum.slogging as slogging
    slogging.configure(config_string=':info,p2p.protocol:info,p2p.peer:info')
except:
    import devp2p.slogging as slogging
log = slogging.get_logger('devp2p_app')


class Devp2pProtocol(BaseProtocol):
    protocol_id = 1
    network_id = 0
    max_cmd_id = 1  # Actually max id is 0, but 0 is the special value.
    name = b'devp2p'
    version = 1

    def __init__(self, peer, service):
        # required by P2PProtocol
        self.config = peer.config
        BaseProtocol.__init__(self, peer, service)


class Devp2pService(WiredService):

    # required by BaseService
    name = 'devp2pservice'
    default_config = dict(example=dict(num_participants=1))

    # required by WiredService
    wire_protocol = Devp2pProtocol  # create for each peer

    def __init__(self, app):
        log.info('Devp2pService init')
        self.config = app.config
        self.address = privtopub_raw(decode_hex(
            self.config['node']['privkey_hex']))
        super(Devp2pService, self).__init__(app)

    '''
    does not follow style because of DevP2P requirement
    '''

    def on_wire_protocol_stop(self, proto):
        log.info(
            'NODE{} on_wire_protocol_stop'.format(self.config['node_num']),
            proto=proto)
        active_peers = self.getConnectedPeers()
        self.app.network.loop.call_soon_threadsafe(
            asyncio.ensure_future,
            self.app.network.refreshConnections(active_peers)
        )

    '''
    does not follow style because of DevP2P requirement
    '''

    def on_wire_protocol_start(self, proto):
        log.info(
            'NODE{} on_wire_protocol_start'.format(self.config['node_num']),
            proto=proto)
        active_peers = self.getConnectedPeers()
        self.app.network.loop.call_soon_threadsafe(
            asyncio.ensure_future,
            self.app.network.refreshConnections(active_peers)
        )

    def getConnectedPeers(self):
        ps = [p for p in self.app.services.peermanager.peers if p]
        aps = [p for p in ps if not p.is_stopped]
        log.info("I am {} I have {} peers: {}".format(
            self.app.config['client_version_string'],
            len(aps),
            [p.remote_client_version if p.remote_client_version != '' else 'Not Ready' for p in aps]
        ))
        return [p.remote_client_version.decode("utf-8") for p in aps if p.remote_client_version != '']

    def start(self):
        log.info('Devp2pService start')
        super(Devp2pService, self).start()


class Devp2pApp(BaseApp):
    '''
    App running on top of DevP2P network, handles node discovery
    '''
    client_name = 'devp2papp'
    version = '0.1'
    client_version = '%s/%s/%s' % (version, sys.platform,
                                   'py%d.%d.%d' % sys.version_info[:3])
    client_version_string = '%s/v%s' % (client_name, client_version)
    default_config = dict(BaseApp.default_config)
    default_config['client_version_string'] = client_version_string
    default_config['post_app_start_callback'] = None

    def __init__(self, config, network):
        self.network = network
        super(Devp2pApp, self).__init__(config)


def serve_app(app):
    app.start()
    app.join()
    app.stop()


def devp2p_app(env, network):

    seed = 0
    gevent.get_hub().SYSTEM_ERROR = BaseException

    # get bootstrap node (node0) enode
    bootstrap_node_privkey = sha3(
        '{}:udp:{}'.format(seed, env.config.DEVP2P_BOOTSTRAP_PORT).encode('utf-8'))
    bootstrap_node_pubkey = privtopub_raw(bootstrap_node_privkey)
    enode = host_port_pubkey_to_uri(
        env.config.DEVP2P_BOOTSTRAP_HOST, env.config.DEVP2P_BOOTSTRAP_PORT, bootstrap_node_pubkey)

    services = [NodeDiscovery, peermanager.PeerManager, Devp2pService]

    # prepare config
    base_config = dict()
    for s in services:
        update_config_with_defaults(base_config, s.default_config)

    base_config['discovery']['bootstrap_nodes'] = [enode]
    base_config['seed'] = seed
    base_config['base_port'] = env.config.DEVP2P_PORT
    base_config['min_peers'] = env.config.DEVP2P_MIN_PEERS
    base_config['max_peers'] = env.config.DEVP2P_MAX_PEERS

    min_peers = base_config['min_peers']
    max_peers = base_config['max_peers']

    assert min_peers <= max_peers
    config = copy.deepcopy(base_config)
    node_num = 0
    config['node_num'] = env.config.DEVP2P_PORT

    # create this node priv_key
    config['node']['privkey_hex'] = encode_hex(sha3(
        '{}:udp:{}'.format(seed, env.config.DEVP2P_PORT).encode('utf-8')))
    # set ports based on node
    config['discovery']['listen_port'] = env.config.DEVP2P_PORT
    config['p2p']['listen_port'] = env.config.DEVP2P_PORT
    config['p2p']['min_peers'] = min(10, min_peers)
    config['p2p']['max_peers'] = max_peers
    ip = network.ip
    config['client_version_string'] = '{}:{}'.format(ip, network.port)

    app = Devp2pApp(config, network)
    log.info('create_app', config=app.config)
    # register services
    for service in services:
        assert issubclass(service, BaseService)
        if service.name not in app.config['deactivated_services']:
            assert service.name not in app.services
            service.register_with_app(app)
            assert hasattr(app.services, service.name)
    serve_app(app)


class P2PNetwork:

    def __init__(self, env, masterServer):
        self.loop = asyncio.get_event_loop()
        self.env = env
        self.activePeerPool = dict()    # peer id => peer
        self.selfId = random_bytes(32)
        self.masterServer = masterServer
        masterServer.network = self
        self.ip = ipaddress.ip_address(
            env.config.DEVP2P_IP if env.config.DEVP2P_IP != '' else socket.gethostbyname(socket.gethostname()))
        self.port = self.env.config.P2P_SERVER_PORT
        self.localPort = self.env.config.LOCAL_SERVER_PORT
        # Internal peer id in the cluster, mainly for connection management
        # 0 is reserved for master
        self.nextClusterPeerId = 0
        self.clusterPeerPool = dict()   # cluster peer id => peer

    async def newPeer(self, client_reader, client_writer):
        peer = Peer(
            self.env,
            client_reader,
            client_writer,
            self,
            self.masterServer,
            self.__getNextClusterPeerId())
        await peer.start(isServer=True)

    async def refreshConnections(self, peers):
        Logger.info("Refreshing connections to {} peers: {}".format(
            len(peers),
            peers
        ))
        # 1. disconnect peers that are not in devp2p peer list
        to_be_disconnected = []
        for peerId, peer in self.activePeerPool.items():
            ip_port = '{}:{}'.format(peer.ip, peer.port)
            if ip_port not in peers:
                to_be_disconnected.append(peer)
        Logger.info("disconnecting peers not in devp2p discovery: {}".format(
            to_be_disconnected
        ))
        for peer in to_be_disconnected:
            peer.close()
        # 2. connect to peers that are in devp2p peer list
        # only initiate connections from smaller of ip_port,
        # to avoid peers trying to connect each other at the same time
        active = ['{}:{}'.format(p.ip, p.port) for i,p in self.activePeerPool.items()]
        to_be_connected = set(peers) - set(active)
        Logger.info("connecting to peers from devp2p discovery: {}".format(
            to_be_connected
        ))
        self_ip_port = '{}:{}'.format(self.ip, self.port)
        for ip_port in to_be_connected:
            if self_ip_port < ip_port:
                ip, port = ip_port.split(':')
                asyncio.ensure_future(self.connect(ip, port))
            else:
                Logger.info("skipping {} to prevent concurrent peer initialization".format(
                    ip_port
                ))

    async def connect(self, ip, port):
        Logger.info("connecting {} {}".format(ip, port))
        try:
            reader, writer = await asyncio.open_connection(ip, port, loop=self.loop)
        except Exception as e:
            Logger.info("failed to connect {} {}: {}".format(ip, port, e))
            return None
        peer = Peer(self.env, reader, writer, self,
                    self.masterServer, self.__getNextClusterPeerId())
        peer.sendHello()
        result = await peer.start(isServer=False)
        if result is not None:
            return None
        return peer

    def iteratePeers(self):
        return self.clusterPeerPool.values()

    def shutdownPeers(self):
        activePeerPool = self.activePeerPool
        self.activePeerPool = dict()
        for peerId, peer in activePeerPool.items():
            peer.close()

    def startServer(self):
        coro = asyncio.start_server(
            self.newPeer, "0.0.0.0", self.port, loop=self.loop)
        self.server = self.loop.run_until_complete(coro)
        Logger.info("Self id {}".format(self.selfId.hex()))
        Logger.info("Listening on {} for p2p".format(
            self.server.sockets[0].getsockname()))

    def shutdown(self):
        self.shutdownPeers()
        self.server.close()
        self.loop.run_until_complete(self.server.wait_closed())

    def start(self):
        self.startServer()

        if self.env.config.LOCAL_SERVER_ENABLE:
            coro = asyncio.start_server(
                self.newLocalClient, "0.0.0.0", self.localPort, loop=self.loop)
            self.local_server = self.loop.run_until_complete(coro)
            Logger.info("Listening on {} for local".format(
                self.local_server.sockets[0].getsockname()))

    # ------------------------------- Cluster Peer Management --------------------------------
    def __getNextClusterPeerId(self):
        self.nextClusterPeerId = self.nextClusterPeerId + 1
        return self.nextClusterPeerId

    def getPeerByClusterPeerId(self, clusterPeerId):
        return self.clusterPeerPool.get(clusterPeerId)


if __name__ == '__main__':
    main()
