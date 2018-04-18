import argparse
import asyncio
import ipaddress
import json

from quarkchain.config import DEFAULT_ENV
from quarkchain.chain import QuarkChainState
from quarkchain.cluster.rpc import ConnectToSlavesRequest, ClusterOp, CLUSTER_OP_SERIALIZER_MAP, Ping, SlaveInfo
from quarkchain.protocol import Connection
from quarkchain.db import PersistentDb
from quarkchain.simple_network import SimpleNetwork
from quarkchain.utils import is_shard_in_mask, set_logging_level, Logger, check


class ClusterConfig:

    def __init__(self, configFile):
        self.config = json.load(open(configFile))

    def getSlaveInfoList(self):
        results = []
        for slave in self.config["slaves"]:
            ip = int(ipaddress.ip_address(slave["ip"]))
            results.append(SlaveInfo(slave["id"], ip, slave["port"], slave["shard_masks"]))
        return results


class SlaveConnection(Connection):
    OP_NONRPC_MAP = {}
    OP_RPC_MAP = {}

    def __init__(self, env, reader, writer, masterServer, slaveId, shardMaskList):
        super().__init__(env, reader, writer, CLUSTER_OP_SERIALIZER_MAP, self.OP_NONRPC_MAP, self.OP_RPC_MAP)
        self.masterServer = masterServer
        self.id = slaveId
        self.shardMaskList = shardMaskList
        check(len(shardMaskList) > 0)

        asyncio.ensure_future(self.activeAndLoopForever())

    def hasShard(self, shardId):
        for shardMask in self.shardMaskList:
            if is_shard_in_mask(shardId, shardMask):
                return True
        return False

    async def sendPing(self):
        req = Ping("", [])
        op, resp, rpcId = await self.writeRpcRequest(ClusterOp.PING, req)
        return (resp.id, resp.shardMaskList)

    async def sendConnectToSlaves(self, slaveInfoList):
        ''' Make slave connect to other slaves.
        Returns True on success
        '''
        req = ConnectToSlavesRequest(slaveInfoList)
        op, resp, rpcId = await self.writeRpcRequest(ClusterOp.CONNECT_TO_SLAVES_REQUEST, req)
        check(len(resp.resultList) == len(slaveInfoList))
        for i, result in enumerate(resp.resultList):
            if len(result) > 0:
                Logger.info("Slave {} failed to connect to {} with error {}".format(
                    self.id, slaveInfoList[i].id, result))
                return False
        Logger.info("Slave {} connected to other slaves successfully".format(self.id))
        return True

    def close(self):
        Logger.info("Lost connection with slave {}".format(self.id))
        super().close()
        self.masterServer.shutdown()

    def closeWithError(self, error):
        Logger.info("Closing connection with slave {}".format(self.id))
        return super().closeWithError(error)


class MasterServer():
    ''' Master node in a cluster
    It does two things to initialize the cluster:
    1. Setup connection with all the slaves in ClusterConfig
    2. Make slaves connect to each other
    '''

    def __init__(self, env, network):
        self.loop = asyncio.get_event_loop()
        self.env = env
        self.network = network
        self.qcState = network.qcState
        self.clusterConfig = env.clusterConfig.CONFIG

        # shard id -> a list of slave running the shard
        self.shardToSlaves = [[] for i in range(self.__getShardSize())]
        self.slavePool = set()

    def __getShardSize(self):
        return self.qcState.getShardSize()

    def __hasAllShards(self):
        ''' Returns True if all the shards have been run by at least one node '''
        return all([len(slaves) > 0 for slaves in self.shardToSlaves])

    async def __connect(self, ip, port):
        ''' Retries until success '''
        Logger.info("Trying to connect {}:{}".format(ip, port))
        while True:
            try:
                reader, writer = await asyncio.open_connection(ip, port, loop=self.loop)
                break
            except Exception as e:
                Logger.info("Failed to connect {} {}: {}".format(ip, port, e))
                await asyncio.sleep(1)
        Logger.info("Connected to {}:{}".format(ip, port))
        return (reader, writer)

    async def __connectToSlaves(self):
        ''' Master connects to all the slaves '''
        for slaveInfo in self.clusterConfig.getSlaveInfoList():
            ip = str(ipaddress.ip_address(slaveInfo.ip))
            reader, writer = await self.__connect(ip, slaveInfo.port)

            slave = SlaveConnection(self.env, reader, writer, self, slaveInfo.id, slaveInfo.shardMaskList)
            await slave.waitUntilActive()

            # Verify the slave does have the same id and shard mask list as the config file
            id, shardMaskList = await slave.sendPing()
            if id != slaveInfo.id:
                Logger.error("Slave id does not match. expect {} got {}".format(slaveInfo.id, id))
                self.shutdown()
            if shardMaskList != slaveInfo.shardMaskList:
                Logger.error("Slave {} shard mask list does not match. expect {} got {}".format(
                    slaveInfo.id, slaveInfo.shardMaskList, shardMaskList))
                self.shutdown()

            self.slavePool.add(slave)
            for shardId in range(self.__getShardSize()):
                if slave.hasShard(shardId):
                    self.shardToSlaves[shardId].append(slave)

    async def __setupSlaveToSlaveConnections(self):
        ''' Make slaves connect to other slaves.
        Retries until success.
        '''
        for slave in self.slavePool:
            await slave.waitUntilActive()
            success = await slave.sendConnectToSlaves(self.clusterConfig.getSlaveInfoList())
            if not success:
                self.shutdown()

    def __logSummary(self):
        for shardId, slaves in enumerate(self.shardToSlaves):
            Logger.info("[{}] is run by slave {}".format(shardId, [s.id for s in slaves]))

    async def __initCluster(self):
        await self.__connectToSlaves()
        self.__logSummary()
        if not self.__hasAllShards():
            Logger.error("Missing some shards. Check cluster config file!")
            return
        await self.__setupSlaveToSlaveConnections()

    def startAndLoop(self):
        self.loop.create_task(self.__initCluster())
        try:
            self.loop.run_forever()
        except KeyboardInterrupt:
            pass

    def shutdown(self):
        self.loop.stop()


def parse_args():
    parser = argparse.ArgumentParser()
    # P2P port
    parser.add_argument(
        "--server_port", default=DEFAULT_ENV.config.P2P_SERVER_PORT, type=int)
    # Local port for JSON-RPC, wallet, etc
    parser.add_argument(
        "--enable_local_server", default=False, type=bool)
    parser.add_argument(
        "--local_port", default=DEFAULT_ENV.config.LOCAL_SERVER_PORT, type=int)
    # Seed host which provides the list of available peers
    parser.add_argument(
        "--seed_host", default=DEFAULT_ENV.config.P2P_SEED_HOST, type=str)
    parser.add_argument(
        "--seed_port", default=DEFAULT_ENV.config.P2P_SEED_PORT, type=int)
    # Node port for intra-cluster RPC
    parser.add_argument(
        "--node_port", default=DEFAULT_ENV.clusterConfig.NODE_PORT, type=int)
    parser.add_argument(
        "--cluster_config", default="cluster_config.json", type=str)
    parser.add_argument("--in_memory_db", default=False)
    parser.add_argument("--db_path", default="./db", type=str)
    parser.add_argument("--log_level", default="info", type=str)
    args = parser.parse_args()

    set_logging_level(args.log_level)

    env = DEFAULT_ENV.copy()
    env.config.P2P_SERVER_PORT = args.server_port
    env.config.P2P_SEED_HOST = args.seed_host
    env.config.P2P_SEED_PORT = args.seed_port
    env.config.LOCAL_SERVER_PORT = args.local_port
    env.config.LOCAL_SERVER_ENABLE = args.enable_local_server
    env.clusterConfig.NODE_PORT = args.node_port
    env.clusterConfig.CONFIG = ClusterConfig(args.cluster_config)
    if not args.in_memory_db:
        env.db = PersistentDb(path=args.db_path, clean=True)

    return env


def main():
    env = parse_args()
    env.NETWORK_ID = 1  # testnet

    qcState = QuarkChainState(env)
    network = SimpleNetwork(env, qcState)
    network.start()

    master = MasterServer(env, network)
    master.startAndLoop()

    Logger.info("Server is shutdown")


if __name__ == '__main__':
    main()
