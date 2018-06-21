import argparse
import asyncio
import json
import os
import tempfile
import signal
import psutil

from asyncio import subprocess

from quarkchain.config import DEFAULT_ENV
from quarkchain.cluster.utils import create_cluster_config
from quarkchain.utils import is_p2

IP = "127.0.0.1"
PORT = 38000


def kill_child_processes(parent_pid, sig=signal.SIGTERM):
    """ Kill all the subprocesses recursively """
    try:
        parent = psutil.Process(parent_pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    print("================================ SHUTTING DOWN CLUSTER ================================")
    for process in children:
        try:
            print("SIGTERM >>> " + " ".join(process.cmdline()[1:]))
        except Exception:
            pass
        process.send_signal(sig)


def dump_config_to_file(config):
    fd, filename = tempfile.mkstemp()
    with os.fdopen(fd, 'w') as tmp:
        json.dump(config, tmp)
    return filename


async def run_master(configFilePath, dbPathRoot, serverPort, jsonRpcPort, seedHost, seedPort, mine, clean, **kwargs):
    cmd = "python master.py --cluster_config={} --db_path_root={} " \
          "--server_port={} --local_port={} --seed_host={} --seed_port={} " \
          "--devp2p_port={} --devp2p_bootstrap_host={} " \
          "--devp2p_bootstrap_port={} --devp2p_min_peers={} --devp2p_max_peers={}".format(
              configFilePath, dbPath, serverPort, jsonRpcPort, seedHost, seedPort,
              kwargs['devp2p_port'], kwargs['devp2p_bootstrap_host'], kwargs['devp2p_bootstrap_port'],
              kwargs['devp2p_min_peers'], kwargs['devp2p_max_peers'])
    if mine:
        cmd += " --mine=true"
    if kwargs['devp2p']:
        cmd += " --devp2p=true"
    if clean:
        cmd += " --clean=true"
    return await asyncio.create_subprocess_exec(*cmd.split(" "), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


async def run_slave(port, id, shardMaskList, dbPathRoot, clean):
    cmd = "pypy3 slave.py --node_port={} --shard_mask={} --node_id={} --db_path_root={}".format(
        port, shardMaskList[0], id, dbPathRoot)
    if clean:
        cmd += " --clean=true"
    return await asyncio.create_subprocess_exec(*cmd.split(" "), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


async def print_output(prefix, stream):
    while True:
        line = await stream.readline()
        if not line:
            break
        print("{}: {}".format(prefix, line.decode("ascii").strip()))


class Cluster:

    def __init__(self, config, configFilePath, mine, clean, clusterID=''):
        self.config = config
        self.configFilePath = configFilePath
        self.procs = []
        self.shutdownCalled = False
        self.mine = mine
        self.clean = clean
        self.clusterID = clusterID

    async def waitAndShutdown(self, prefix, proc):
        ''' If one process terminates shutdown the entire cluster '''
        await proc.wait()
        if self.shutdownCalled:
            return

        print("{} is dead. Shutting down the cluster...".format(prefix))
        await self.shutdown()

    async def runMaster(self):
        master = await run_master(
            configFilePath=self.configFilePath,
            dbPathRoot=self.config["master"]["db_path_root"],
            serverPort=self.config["master"]["server_port"],
            jsonRpcPort=self.config["master"]["json_rpc_port"],
            seedHost=self.config["master"]["seed_host"],
            seedPort=self.config["master"]["seed_port"],
            mine=self.mine,
            clean=self.clean,
            devp2p=self.config["master"]["devp2p"],
            devp2p_port=self.config["master"]["devp2p_port"],
            devp2p_bootstrap_host=self.config["master"]["devp2p_bootstrap_host"],
            devp2p_bootstrap_port=self.config["master"]["devp2p_bootstrap_port"],
            devp2p_min_peers=self.config["master"]["devp2p_min_peers"],
            devp2p_max_peers=self.config["master"]["devp2p_max_peers"])
        prefix = "{}MASTER".format(self.clusterID)
        asyncio.ensure_future(print_output(prefix, master.stdout))
        self.procs.append((prefix, master))

    async def runSlaves(self):
        for slave in self.config["slaves"]:
            s = await run_slave(
                port=slave["port"],
                id=slave["id"],
                shardMaskList=slave["shard_masks"],
                dbPathRoot=slave["db_path_root"],
                clean=self.clean)
            prefix = "{}SLAVE_{}".format(self.clusterID, slave["id"])
            asyncio.ensure_future(print_output(prefix, s.stdout))
            self.procs.append((prefix, s))

    async def run(self):
        await self.runMaster()
        await self.runSlaves()

        await asyncio.gather(*[self.waitAndShutdown(prefix, proc) for prefix, proc in self.procs])

    async def shutdown(self):
        self.shutdownCalled = True
        kill_child_processes(os.getpid())

    def startAndLoop(self):
        try:
            asyncio.get_event_loop().run_until_complete(self.run())
        except KeyboardInterrupt:
            try:
                asyncio.get_event_loop().run_until_complete(self.shutdown())
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cluster_config", default="cluster_config.json", type=str)
    parser.add_argument(
        "--num_slaves", default=4, type=int)
    parser.add_argument(
        "--mine", default=False, type=bool)
    parser.add_argument(
        "--port_start", default=PORT, type=int)
    parser.add_argument(
        "--db_path_root", default="./", type=str) # default to current dir
    parser.add_argument(
        "--p2p_port", default=DEFAULT_ENV.config.P2P_SERVER_PORT)
    parser.add_argument(
        "--json_rpc_port", default=38391, type=int)
    parser.add_argument(
        "--seed_host", default=DEFAULT_ENV.config.P2P_SEED_HOST)
    parser.add_argument(
        "--seed_port", default=DEFAULT_ENV.config.P2P_SEED_PORT)
    parser.add_argument(
        "--clean", default=False)
    parser.add_argument(
        "--devp2p", default=False, type=bool)
    parser.add_argument(
        "--devp2p_port", default=29000, type=int)
    parser.add_argument(
        "--devp2p_bootstrap_host", default='0.0.0.0', type=str)
    parser.add_argument(
        "--devp2p_bootstrap_port", default=29000, type=int)
    parser.add_argument(
        "--devp2p_min_peers", default=2, type=int)
    parser.add_argument(
        "--devp2p_max_peers", default=10, type=int)

    args = parser.parse_args()

    if args.num_slaves <= 0:
        config = json.load(open(args.cluster_config))
        filename = args.cluster_config
    else:
        config = create_cluster_config(
            slaveCount=args.num_slaves,
            ip=IP,
            p2pPort=args.p2p_port,
            clusterPortStart=args.port_start,
            jsonRpcPort=args.json_rpc_port,
            seedHost=args.seed_host,
            seedPort=args.seed_port,
            dbPathRoot = args.db_path_root,
            devp2p=args.devp2p,
            devp2p_port=args.devp2p_port,
            devp2p_bootstrap_host=args.devp2p_bootstrap_host,
            devp2p_bootstrap_port=args.devp2p_bootstrap_port,
            devp2p_min_peers=args.devp2p_min_peers,
            devp2p_max_peers=args.devp2p_max_peers,
        )
        if not config:
            return -1
        filename = dump_config_to_file(config)

    cluster = Cluster(config, filename, args.mine, args.clean)
    cluster.startAndLoop()


if __name__ == '__main__':
    main()
