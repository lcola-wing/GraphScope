#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2020-2023 Alibaba Group Holding Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from typing import List

from graphscope.framework.utils import PipeWatcher
from graphscope.framework.utils import get_free_port
from graphscope.framework.utils import get_java_version
from graphscope.framework.utils import get_tempdir
from graphscope.framework.utils import is_free_port
from graphscope.proto import types_pb2

from gscoordinator.launcher import AbstractLauncher
from gscoordinator.utils import ANALYTICAL_ENGINE_PATH
from gscoordinator.utils import GRAPHSCOPE_HOME
from gscoordinator.utils import INTERACTIVE_ENGINE_SCRIPT
from gscoordinator.utils import INTERACTIVE_ENGINE_THREADS_PER_WORKER
from gscoordinator.utils import WORKSPACE
from gscoordinator.utils import ResolveMPICmdPrefix
from gscoordinator.utils import get_timestamp
from gscoordinator.utils import parse_as_glog_level
from gscoordinator.utils import run_command

logger = logging.getLogger("graphscope")


class LocalLauncher(AbstractLauncher):
    def __init__(
        self,
        num_workers: int,
        hosts: str,
        etcd_addrs: str,
        etcd_listening_client_port: int,
        etcd_listening_peer_port: int,
        vineyard_socket: str,
        shared_mem: str,
        log_level: str,
        instance_id: str,
        timeout_seconds: int,
        close_timeout_seconds: int = 60,
        retry_time_seconds: int = 1,
    ):
        super().__init__()
        self._num_workers = num_workers
        self._hosts = hosts

        self._external_etcd_addr = etcd_addrs
        self._etcd_listening_client_port = etcd_listening_client_port
        self._etcd_listening_peer_port = etcd_listening_peer_port
        self._vineyard_socket = vineyard_socket
        self._shared_mem = shared_mem

        self._glog_level = parse_as_glog_level(log_level)
        self._instance_id = instance_id
        self._timeout_seconds = timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._retry_time_seconds = retry_time_seconds
        self._vineyard_socket_prefix = os.path.join(get_tempdir(), "vineyard.sock.")

        # A graphscope instance may have multiple session by reconnecting to coordinator
        self._instance_workspace = os.path.join(WORKSPACE, self._instance_id)
        os.makedirs(self._instance_workspace, exist_ok=True)
        # setting during client connect to coordinator
        self._session_workspace = None

        # etcd
        self._etcd_peer_port = None
        self._etcd_client_port = None
        self._etcd_process = None
        self._etcd_endpoint = None
        # vineyardd
        self._vineyard_rpc_port = None
        self._vineyardd_process = None
        # analytical engine
        self._analytical_engine_process = None

        # interactive engine
        # executor inter-processing port
        # executor rpc port
        # frontend port
        self._interactive_port = 8233
        while not is_free_port(self._interactive_port):
            self._interactive_port += 10

        # learning instance processes
        self._learning_instance_processes = {}

    def type(self):
        return types_pb2.HOSTS

    def stop(self, is_dangling=False):
        self.close_analytical_instance()
        self.close_vineyard()
        self.close_etcd()

    def set_session_workspace(self, session_id):
        self._session_workspace = os.path.join(self._instance_workspace, session_id)
        os.makedirs(self._session_workspace, exist_ok=True)

    def get_namespace(self):
        return ""

    @property
    def hosts(self):
        return self._hosts

    @property
    def vineyard_socket(self):
        return self._vineyard_socket

    @property
    def etcd_port(self):
        return self._etcd_client_port

    def create_analytical_instance(self):
        mpi_resolver = ResolveMPICmdPrefix()
        cmd, mpi_env = mpi_resolver.resolve(self._num_workers, self._hosts)

        master = self.hosts.split(",")[0]
        rpc_port = get_free_port(master)
        self._analytical_engine_endpoint = f"{master}:{rpc_port}"

        cmd.append(ANALYTICAL_ENGINE_PATH)
        cmd.extend(["--host", "0.0.0.0"])
        cmd.extend(["--port", str(rpc_port)])
        cmd.extend(["--vineyard_shared_mem", self._shared_mem])

        if mpi_resolver.openmpi():
            cmd.extend(["-v", str(self._glog_level)])
        else:
            mpi_env["GLOG_v"] = str(self._glog_level)

        if self.vineyard_socket is not None:
            cmd.extend(["--vineyard_socket", self.vineyard_socket])

        env = os.environ.copy()
        env.update(mpi_env)
        env["GRAPHSCOPE_HOME"] = GRAPHSCOPE_HOME

        logger.info("Launch analytical engine with command: %s", " ".join(cmd))

        process = subprocess.Popen(
            cmd,
            start_new_session=True,
            cwd=os.getcwd(),
            env=env,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1,
        )

        logger.info("Server is initializing analytical engine.")
        stdout_watcher = PipeWatcher(process.stdout, sys.stdout)
        stderr_watcher = PipeWatcher(process.stderr, sys.stderr)
        setattr(process, "stdout_watcher", stdout_watcher)
        setattr(process, "stderr_watcher", stderr_watcher)

        self._analytical_engine_process = process

        start_time = time.time()
        while is_free_port(rpc_port):
            if self._timeout_seconds + start_time < time.time():
                self._analytical_engine_process.kill()
                raise RuntimeError("Launch analytical engine failed due to timeout.")
            time.sleep(self._retry_time_seconds)

        logger.info(
            "Analytical engine is listening on %s", self._analytical_engine_endpoint
        )

    def create_interactive_instance(
        self, object_id: int, schema_path: str, params: dict
    ):
        try:
            logger.info("Java version: %s", get_java_version())
        except:  # noqa: E722
            logger.exception("Cannot get version of java")

        env = os.environ.copy()
        env["GRAPHSCOPE_HOME"] = GRAPHSCOPE_HOME
        if ".install_prefix" in INTERACTIVE_ENGINE_SCRIPT:
            env["GRAPHSCOPE_HOME"] = os.path.dirname(
                os.path.dirname(INTERACTIVE_ENGINE_SCRIPT)
            )

        if os.environ.get("PARALLEL_INTERACTIVE_EXECUTOR_ON_VINEYARD", "OFF") != "ON":
            # only one GIE/GAIA executor will be launched locally, even there are
            # multiple GAE engines
            num_workers = 1
            threads_per_worker = int(
                os.environ.get(
                    "THREADS_PER_WORKER", INTERACTIVE_ENGINE_THREADS_PER_WORKER
                )
            )
            env["THREADS_PER_WORKER"] = str(threads_per_worker * self._num_workers)
        else:
            num_workers = self._num_workers

        params = "\n".join([f"{k}={v}" for k, v in params.items()])
        params = base64.b64encode(params.encode("utf-8")).decode("utf-8")

        cmd = [
            INTERACTIVE_ENGINE_SCRIPT,
            "create_gremlin_instance_on_local",
            self._session_workspace,
            str(object_id),
            schema_path,
            str(num_workers),  # server size
            str(self._interactive_port),  # executor port
            str(self._interactive_port + 1),  # executor rpc port
            str(self._interactive_port + 2 * num_workers),  # frontend gremlin port
            str(self._interactive_port + 2 * num_workers + 1),  # frontend cypher port
            self.vineyard_socket,
            params,
        ]
        logger.info("Create GIE instance with command: %s", " ".join(cmd))
        self._interactive_port += 2 * num_workers + 2
        process = subprocess.Popen(
            cmd,
            start_new_session=True,
            cwd=os.getcwd(),
            env=env,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
        return process

    def create_learning_instance(self, object_id, handle, config):
        # prepare argument
        handle = json.loads(
            base64.b64decode(handle.encode("utf-8", errors="ignore")).decode(
                "utf-8", errors="ignore"
            )
        )

        server_list = [
            f"localhost:{get_free_port('localhost')}" for _ in range(self.num_workers)
        ]
        hosts = ",".join(server_list)
        handle["server"] = hosts
        handle = base64.b64encode(
            json.dumps(handle).encode("utf-8", errors="ignore")
        ).decode("utf-8", errors="ignore")

        # launch the server
        env = os.environ.copy()
        # set coordinator dir to PYTHONPATH
        python_path = (
            env.get("PYTHONPATH", "")
            + os.pathsep
            + os.path.dirname(os.path.dirname(__file__))
        )
        env["PYTHONPATH"] = python_path

        self._learning_instance_processes[object_id] = []
        for index in range(self._num_workers):
            cmd = [
                sys.executable,
                "-m",
                "gscoordinator.learning",
                handle,
                config,
                str(index),
            ]
            logger.debug("launching learning server: %s", " ".join(cmd))

            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                universal_newlines=True,
                bufsize=1,
            )
            stdout_watcher = PipeWatcher(
                proc.stdout,
                sys.stdout,
                suppressed=(not logger.isEnabledFor(logging.DEBUG)),
            )
            setattr(proc, "stdout_watcher", stdout_watcher)
            self._learning_instance_processes[object_id].append(proc)
        return server_list

    def close_analytical_instance(self):
        self._stop_subprocess(self._analytical_engine_process, kill=True)
        self._analytical_engine_endpoint = None

    def close_interactive_instance(self, object_id):
        env = os.environ.copy()
        env["GRAPHSCOPE_HOME"] = GRAPHSCOPE_HOME
        if ".install_prefix" in INTERACTIVE_ENGINE_SCRIPT:
            env["GRAPHSCOPE_HOME"] = os.path.dirname(
                os.path.dirname(INTERACTIVE_ENGINE_SCRIPT)
            )
        cmd = [
            INTERACTIVE_ENGINE_SCRIPT,
            "close_gremlin_instance_on_local",
            self._session_workspace,
            str(object_id),
        ]
        logger.info("Close GIE instance with command: %s", " ".join(cmd))
        process = subprocess.Popen(
            cmd,
            start_new_session=True,
            cwd=os.getcwd(),
            env=env,
            encoding="utf-8",
            errors="replace",
            universal_newlines=True,
            bufsize=1,
        )
        # 60 seconds is enough
        process.wait(timeout=self._close_timeout_seconds)
        return process

    def close_learning_instance(self, object_id):
        if object_id not in self._learning_instance_processes:
            return

        # terminate the process
        for proc in self._learning_instance_processes[object_id]:
            self._stop_subprocess(proc, kill=True)
        self._learning_instance_processes.clear()

    def launch_etcd(self):
        if is_free_port(self._etcd_listening_client_port):
            self._etcd_client_port = self._etcd_listening_client_port
        else:
            self._etcd_client_port = get_free_port()
        if is_free_port(self._etcd_listening_peer_port):
            self._etcd_peer_port = self._etcd_listening_peer_port
        else:
            self._etcd_peer_port = get_free_port()

        if isinstance(self._hosts, (list, tuple)):
            hosts = self._hosts
        else:
            hosts = self._hosts.split(",")
        local_hostname = "127.0.0.1"
        if len(hosts) > 1:
            try:
                local_hostname = socket.gethostname()
                socket.gethostbyname(
                    local_hostname
                )  # make sure the hostname is dns-resolvable
            except:  # noqa: E722
                local_hostname = "127.0.0.1"  # fallback to a must-correct hostname

        self._etcd_endpoint = f"http://{local_hostname}:{self._etcd_client_port}"

        env = os.environ.copy()
        env.update({"ETCD_MAX_TXN_OPS": "102400"})
        etcd_exec = self.find_etcd()
        cmd = etcd_exec + [
            "--data-dir",
            str(self._instance_workspace),
            "--listen-peer-urls",
            f"http://0.0.0.0:{self._etcd_peer_port}",
            "--listen-client-urls",
            f"http://0.0.0.0:{self._etcd_client_port}",
            "--advertise-client-urls",
            self._etcd_endpoint,
            "--initial-cluster",
            f"default=http://127.0.0.1:{self._etcd_peer_port}",
            "--initial-advertise-peer-urls",
            f"http://127.0.0.1:{self._etcd_peer_port}",
        ]
        logger.info("Launch etcd with command: %s", " ".join(cmd))
        logger.info("Server is initializing etcd.")

        process = subprocess.Popen(
            cmd,
            start_new_session=True,
            cwd=os.getcwd(),
            env=env,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )

        stdout_watcher = PipeWatcher(
            process.stdout,
            sys.stdout,
            drop=False,
            suppressed=False,
        )
        setattr(process, "stdout_watcher", stdout_watcher)
        self._etcd_process = process

        start_time = time.time()
        while is_free_port(self._etcd_client_port):
            if self._timeout_seconds + start_time < time.time():
                self._etcd_process.kill()
                _, errs = self._etcd_process.communicate()
                logger.error("Start etcd timeout, %s", errs)
                msg = "Launch etcd service failed due to timeout: "
                msg += "\n".join([line for line in stdout_watcher.poll_all()])
                raise RuntimeError(msg)
            time.sleep(self._retry_time_seconds)

        stdout_watcher.drop(True)
        stdout_watcher.suppress(not logger.isEnabledFor(logging.DEBUG))
        logger.info("Etcd is ready, endpoint is %s", self._etcd_endpoint)

    def launch_vineyard(self):
        if self.vineyard_socket is not None:
            logger.info("Found existing vineyard socket: %s", self.vineyard_socket)
            return

        hosts = [f"{host.split(':')[0]}:1" for host in self._hosts.split(",")]

        if len(hosts) > 1:  # Use MPI to start multiple process
            mpi_resolver = ResolveMPICmdPrefix()
            cmd, mpi_env = mpi_resolver.resolve(len(hosts), ",".join(hosts))
        else:  # Start single process without MPI
            cmd, mpi_env = [], {}

        ts = get_timestamp()
        self._vineyard_socket = f"{self._vineyard_socket_prefix}{ts}"
        self._vineyard_rpc_port = 9600 if is_free_port(9600) else get_free_port()

        cmd.extend([sys.executable, "-m", "vineyard"])
        cmd.extend(["--socket", self.vineyard_socket])
        cmd.extend(["--rpc_socket_port", str(self._vineyard_rpc_port)])
        cmd.extend(["--size", self._shared_mem])
        if len(hosts) == 1:
            cmd.extend(["--meta", "local"])
        else:
            cmd.extend(["-etcd_endpoint", self._etcd_endpoint])
            cmd.extend(["-etcd_prefix", f"vineyard.gsa.{ts}"])
        env = os.environ.copy()
        env["GLOG_v"] = str(self._glog_level)
        env.update(mpi_env)

        logger.info("Launch vineyardd with command: %s", " ".join(cmd))
        logger.info("Server is initializing vineyardd.")

        process = subprocess.Popen(
            cmd,
            start_new_session=True,
            cwd=os.getcwd(),
            env=env,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )

        stdout_watcher = PipeWatcher(
            process.stdout,
            sys.stdout,
            drop=False,
            suppressed=False,
        )
        setattr(process, "stdout_watcher", stdout_watcher)
        self._vineyardd_process = process

        start_time = time.time()
        if len(hosts) > 1:
            time.sleep(5 * self._retry_time_seconds)  # should be OK
        else:
            while not os.path.exists(self._vineyard_socket):
                if self._vineyardd_process.poll() is not None:
                    msg = "Launch vineyardd failed: "
                    msg += "\n".join([line for line in stdout_watcher.poll_all()])
                    msg += "\nRerun with `graphscope.set_option(log_level='debug')`,"
                    msg += " to get verbosed vineyardd logs."
                    raise RuntimeError(msg)
                if self._timeout_seconds + start_time < time.time():
                    self._vineyardd_process.kill()
                    # outs, _ = self._vineyardd_process.communicate()
                    # logger.error("Start vineyardd timeout, %s", outs)
                    raise RuntimeError("Launch vineyardd failed due to timeout.")
                time.sleep(self._retry_time_seconds)

        stdout_watcher.drop(True)
        stdout_watcher.suppress(not logger.isEnabledFor(logging.DEBUG))
        logger.info(
            "Vineyardd is ready, ipc socket is %s",
            self._vineyard_socket,
        )

    def close_etcd(self):
        self._stop_subprocess(self._etcd_process)

    def close_vineyard(self):
        self._stop_subprocess(self._vineyardd_process, kill=True)

    @staticmethod
    def _stop_subprocess(proc, kill=False) -> None:
        if proc is not None:
            if kill:
                proc.kill()
            else:
                proc.terminate()

    def distribute_file(self, path) -> None:
        dir = os.path.dirname(path)
        for host in self.hosts.split(","):
            if host not in ("localhost", "127.0.0.1"):
                logger.debug(run_command(f"ssh {host} mkdir -p {dir}"))  # noqa: G004
                logger.debug(run_command(f"scp -r {path} {host}:{path}"))  # noqa: G004

    @staticmethod
    def find_etcd() -> List[str]:
        etcd = shutil.which("etcd")
        if etcd is None:
            etcd = [sys.executable, "-m", "etcd_distro.etcd"]
        else:
            etcd = [etcd]
        return etcd

    def configure_etcd_endpoint(self):
        if self._external_etcd_addr is None:
            self.launch_etcd()
            logger.info("etcd cluster created")
        else:
            self._etcd_endpoint = f"http://{self._external_etcd_addr}"
            logger.info("Using etcd cluster")
        logger.info("etcd endpoint is %s", self._etcd_endpoint)

    def start(self):
        try:
            # create etcd
            if isinstance(self._hosts, (list, tuple)):
                hosts = self._hosts
            else:
                hosts = self._hosts.split(",")
            if len(hosts) > 1:
                self.configure_etcd_endpoint()
            # create vineyard
            self.launch_vineyard()
        except Exception:  # pylint: disable=broad-except
            time.sleep(1)
            logger.exception("Error when launching GraphScope on local")
            self.stop()
            return False
        return True

    def get_engine_config(self) -> dict:
        config = {
            "engine_hosts": self._hosts,
            "mars_endpoint": None,
        }
        return config

    def get_vineyard_stream_info(self):
        return "ssh", self.hosts.split(",")
