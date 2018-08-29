'''mpi4py wrapper that allows an ensemble of serial applications to run in
parallel across ranks on the computing resource'''
import argparse
from collections import defaultdict
import os
import sys
import logging
import django
import random
import signal
import time

os.environ['DJANGO_SETTINGS_MODULE'] = 'balsam.django_config.settings'
django.setup()
logger = logging.getLogger('balsam.launcher.mpi_ensemble')

from django.db import transaction
from subprocess import Popen, STDOUT, TimeoutExpired

from mpi4py import MPI

from balsam.launcher.util import cd, get_tail, remaining_time_minutes
from balsam.launcher.exceptions import *
from balsam.service.models import BalsamJob

comm = MPI.COMM_WORLD
RANK = comm.Get_rank()
django.db.connections.close_all()

class ResourceManager:

    FETCH_PERIOD = 2.0
    KILLED_REFRESH_PERIOD = 3.0

    def __init__(self, job_source):
        self.job_source = job_source
        self.node_occupancy = [0.0 for i in range(comm.size)]
        self.node_occupancy[0] = 1.0
        self.running_locations = {}
        self.job_occupancy = {}

        self.last_job_fetch = -10.0
        self.last_killed_refresh = -10.0
        self.job_cache = []
        self.killed_pks = []

        self.recv_requests = {i:comm.irecv(source=i) for i in range(1,comm.size)}

        if self.job_source.qLaunch is not None:
            sched_id = self.job_source.qLaunch.scheduler_id
            self.RUN_MESSAGE = f'Scheduled by Balsam Service (Scheduler ID: {sched_id})'
        else:
            self.RUN_MESSAGE = 'Not scheduled by Balsam service'
        logger.info(self.RUN_MESSAGE)
        logger.info(f'Assigning jobs to {comm.size-1} worker ranks')

    def refresh_job_cache(self):
        now = time.time()
        if len(self.job_cache) == 0 or (now-self.last_job_fetch) > self.FETCH_PERIOD:
            jobquery = self.job_source.get_runnable(
                max_nodes=1,
                serial_only=True,
                order_by='node_packing_count' # ascending
            )
            self.job_cache = list(jobquery)
            self.last_job_fetch = now
            logger.debug(f"Refreshed job cache: {len(self.job_cache)} runnable")

    def refresh_killed_jobs(self):
        now = time.time()
        if now - self.last_killed_refresh > self.KILLED_REFRESH_PERIOD:
            killed_pks = self.job_source.filter(state='USER_KILLED').values_list('job_id', flat=True)

            if len(killed_pks) > len(self.killed_pks): 
                logger.debug(f"Killed jobs: {self.killed_pks}")
            self.killed_pks = killed_pks
            self.last_killed_refresh = now
        
    def pre_assign(self, rank, job):
        job_occ = 1.0 / job.node_packing_count
        self.node_occupancy[rank] += job_occ
        self.job_occupancy[job.pk] = job_occ
        self.running_locations[job.pk] = rank

    def revert_assign(self, rank, job_pk):
        job_occ = self.job_occupancy[job_pk]
        self.node_occupancy[rank] -= job_occ
        del self.job_occupancy[job_pk]
        del self.running_locations[job_pk]

    @transaction.atomic
    def allocate_next_jobs(self):
        '''Generator: yield (job,rank) pairs and mark the nodes/ranks as busy'''
        self.refresh_job_cache()
        send_requests = []
        pre_assignments = defaultdict(list)
        min_packing_count = 1

        for job in self.job_cache:
            if job.node_packing_count < min_packing_count: continue
            job_occ = 1.0 / job.node_packing_count
            
            free_ranks = (i for i in range(1, comm.size) 
                          if self.node_occupancy[i]+job_occ < 1.0001)
            rank = next(free_ranks, None)

            if rank is None:
                logger.debug(f'no free ranks to assign {job.cute_id}')
                min_packing_count = job.node_packing_count + 1
            else:
                pre_assignments[rank].append(job)
                self.pre_assign(rank, job)

        if len(pre_assignments) == 0: return False

        to_acquire = [job.pk for rank in pre_assignments 
                      for job in pre_assignments[rank]]
        acquired_pks = self.job_source.acquire(to_acquire).values_list('job_id', flat=True)
        acquired_pks = list(acquired_pks)
        logger.info(f'Acquired lock on {len(acquired_pks)} out of {len(to_acquire)} jobs marked for running')

        # Make actual assignment:
        for (rank, pre_jobs) in pre_assignments.items():
            runjobs = []
            for j in pre_jobs:
                if j.pk in acquired_pks: 
                    runjobs.append(j)
                    self.job_cache.remove(j)
                else:
                    self.revert_assign(rank, j.pk)

            if runjobs:
                mpiReq = self._send_jobs(runjobs, rank)
                logger.debug(f"Sent {len(runjobs)} jobs to rank {rank}: occupancy is now {self.node_occupancy[rank]}")
                send_requests.append(mpiReq)

        BalsamJob.batch_update_state(acquired_pks, 'RUNNING', self.RUN_MESSAGE)
        MPI.Request.waitall(send_requests)
        return len(acquired_pks) > 0

    def _send_jobs(self, jobs, rank):
        '''Send message to compute rank'''
        message = {}
        message['tag'] = 'NEW'
        for job in jobs:
            job_spec = dict(
                workdir=job.working_directory,
                name=job.name,
                cuteid=job.cute_id,
                cmd=job.app_cmd,
                envs=job.get_envs()
            )
            message[job.pk] = job_spec

        req = comm.isend(message, dest=rank)
        return req

    def _get_requests(self):
        completed_requests = []
        stat = MPI.Status()
        for rank in self.recv_requests:
            req = self.recv_requests[rank]
            done, msg = req.test(status = stat)
            if done: 
                completed_requests.append((stat.source, msg))
                assert stat.source == rank

        for rank,msg in completed_requests:
            self.recv_requests[rank] = comm.irecv(source=rank)
        return completed_requests

    @transaction.atomic
    def serve_requests(self):
        requests = self._get_requests()
        done_jobs = []
        error_jobs = []
        killed_pks = []
        send_reqs = []
        for rank, msg in requests:
            kill_pks, req = self._handle_ask(rank, msg['ask'])
            killed_pks.extend(kill_pks)
            send_reqs.append(req)
            done_jobs.extend(msg['done'])
            error_jobs.extend(msg['error'])

        if done_jobs:  self._handle_dones(done_jobs)
        if error_jobs: self._handle_errors(error_jobs)
        if killed_pks: self.job_source.release(killed_pks)
        MPI.Request.waitall(send_reqs)
        return len(requests)
        
    def _handle_ask(self, rank, ask_pks):
        self.refresh_killed_jobs()
        response = {'tag': 'CONTINUE', 'kill_pks': []}

        for pk in ask_pks:
            if pk in self.killed_pks:
                response['tag'] = 'KILL'
                response['kill_pks'].append(pk)
        req = comm.isend(response, dest=rank)

        for pk in response['kill_pks']:
            self.revert_assign(rank, pk)

        if response['tag'] == 'KILL':
            logger.debug(f"Sent KILL to rank {rank} for {response['kill_pks']}\n"
                         f"occupancy is now {self.node_occupancy[rank]}")

        return response['kill_pks'], req
    
    def _handle_dones(self, done_pks):
        for pk in done_pks:
            rank = self.running_locations[pk]
            self.revert_assign(rank, pk)

        BalsamJob.batch_update_state(done_pks, 'RUN_DONE')
        self.job_source.release(done_pks)
        logger.debug(f"RUN_DONE: {len(done_pks)} jobs")
    
    def _handle_errors(self, error_jobs):
        for pk,retcode,tail in error_jobs:
            rank = self.running_locations[pk]
            self.revert_assign(rank, pk)
            job = BalsamJob.objects.get(pk=pk)
            state_msg = f"nonzero return {retcode}: {tail}"
            job.update_state('RUN_ERROR', state_msg)
            logger.error(f"{job.cute_id} RUN_ERROR from rank {rank}")
            logger.error(state_msg)

        error_pks = [j[0] for j in error_jobs]
        self.job_source.release(error_pks)
    
    def send_exit(self):
        logger.info(f"send_exit: waiting on all pending recvs")
        active_ranks = list(set(self.running_locations.values()))
        requests = [self.recv_requests[i] for i in active_ranks]
        MPI.Request.waitall(requests)
        reqs = []
        logger.info(f"send_exit: send EXIT tag to all ranks")
        for i in range(1, comm.size):
            req = comm.isend({'tag': 'EXIT'}, dest=i)
            reqs.append(req)
        MPI.Request.waitall(reqs)

class Master:
    def __init__(self):
        self.MAX_IDLE_TIME = 10.0
        self.DELAY_PERIOD = 1.0
        self.idle_time = 0.0
        self.EXIT_FLAG = False

        args = self.parse_args()
        comm.bcast(args.gpus_per_node, root=0)
        self.remaining_timer = remaining_time_minutes(args.time_limit_min)
        next(self.remaining_timer)
        
        job_source = BalsamJob.source
        job_source.workflow = args.wf_name
        job_source.start_tick()
        job_source.clear_stale_locks()
        self.manager = ResourceManager(job_source)

        if job_source.workflow:
            logger.info(f'MPI Ensemble pulling jobs with WF {args.wf_name}')
        else:
            logger.info('MPI Ensemble consuming jobs matching any WF name')

    def parse_args(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('--wf-name')
        parser.add_argument('--time-limit-min', type=float, default=72.*60)
        parser.add_argument('--gpus-per-node', type=int, default=0)
        return parser.parse_args()

    def exit(self):
        outstanding_job_pks = list(self.manager.running_locations.keys())
        num_timeout = len(outstanding_job_pks)
        logger.info(f"Shutting down with {num_timeout} jobs still running..timing out")
        BalsamJob.batch_update_state(outstanding_job_pks, 'RUN_TIMEOUT', 'timed out in MPI Ensemble')
        self.manager.job_source.release_all_owned()
        self.manager.send_exit()
        logger.debug("Send_exit: master done")
        logger.debug(f"master calling MPI Finalize")
        MPI.Finalize()
        logger.info(f"ensemble master exit gracefully")
        sys.exit(0)

    def main(self):
        for remaining_minutes in self.remaining_timer:
            self._main()
            if self.EXIT_FLAG:
                logger.info("EXIT_FLAG on; master breaking main loop")
                break
            if self.idle_time > self.MAX_IDLE_TIME and not self.manager.running_locations:
                logger.info(f"Nothing to do for {self.MAX_IDLE_TIME} seconds: quitting")
                break
        self.exit()

    def _main(self):
        ran_anything = False
        got_requests = 0

        ran_anything = self.manager.allocate_next_jobs()
        start = time.time()
        got_requests = self.manager.serve_requests()
        elapsed = time.time() - start
        if got_requests: logger.debug(f"Served {got_requests} requests in {elapsed:.3f} seconds")

        if not (ran_anything or got_requests):
            time.sleep(self.DELAY_PERIOD)
            self.idle_time += self.DELAY_PERIOD
        else:
            self.idle_time = 0.0

class FailedToStartProcess:
    returncode = 12345
    def wait(self, timeout=0): return 12345
    def poll(self, timeout=0): return 12345
    def communicate(self, timeout=0): pass
    def terminate(self): pass
    def kill(self): pass

class Worker:
    CHECK_PERIOD=10
    RETRY_WINDOW = 20
    RETRY_CODES = [-11, 12345]
    MAX_RETRY = 3

    def __init__(self):
        self.processes = {}
        self.outfiles = {}
        self.cuteids = {}
        self.start_times = {}
        self.retry_counts = {}
        self.job_specs = {}
    
    def _cleanup_proc(self, pk, timeout=0):
        self._kill(pk, timeout=timeout)
        self.processes[pk].communicate()
        self.outfiles[pk].close()
        for d in (self.processes, self.outfiles, self.cuteids, self.start_times,
                  self.retry_counts, self.job_specs):
            del d[pk]
    
    def _check_retcode(self, proc, timeout):
        try: 
            retcode = proc.wait(timeout=timeout)
        except TimeoutExpired:
            retcode = None
        return retcode

    def _check_retcodes(self):
        start = time.time()
        pk_retcodes = []
        for pk, proc in self.processes.items():
            elapsed = time.time() - start
            timeout = max(0, self.CHECK_PERIOD - elapsed)
            retcode = self._check_retcode(proc, timeout)
            pk_retcodes.append((pk, retcode))
        return pk_retcodes

    def _log_error_tail(self, pk, retcode):
        fname = self.outfiles[pk].name
        if os.path.exists(fname):
            tail = get_tail(self.outfiles[pk].name)
        else:
            tail = ''
        logmsg = self.log_prefix(pk) + f'nonzero return {retcode}:\n {tail}'
        logger.error(logmsg)
        return tail
           
    def _can_retry(self, pk, retcode):
        if retcode in self.RETRY_CODES:
            elapsed = time.time() - self.start_times[pk]
            retry_count = self.retry_counts[pk]
            if elapsed < self.RETRY_WINDOW and retry_count <= self.MAX_RETRY:
                logmsg = self.log_prefix(pk) 
                logmsg += (f'can retry task (err occured after {elapsed:.2f} sec; '
                          f'attempt {self.retry_counts[pk]}/{self.MAX_RETRY})')
                logger.error(logmsg)
                return True
        return False

    def _kill(self, pk, timeout=0):
        p = self.processes[pk]
        if p.poll() is None:
            p.terminate()
            logger.debug(f"rank {RANK} sent TERM to {self.cuteids[pk]}...waiting on shutdown")
            try: p.wait(timeout=timeout)
            except TimeoutExpired: p.kill()

    def _launch_proc(self, pk):
        job_spec = self.job_specs[pk]
        workdir = job_spec['workdir']
        name = job_spec['name']
        cmd = job_spec['cmd']
        envs = job_spec['envs']

        if type(cmd) is str: cmd = cmd.split()

        if self.gpus_per_node > 0:
            idx = list(self.job_specs.keys()).index(pk) 
            gpu_device = idx % self.gpus_per_node
            envs['CUDA_DEVICE_ORDER'] = "PCI_BUS_ID"
            envs['CUDA_VISIBLE_DEVICES'] = str(gpu_device)

        out_name = f'{name}.out'
        logger.debug(self.log_prefix(pk) + f"\nPopen: {cmd}")

        if not os.path.exists(workdir): os.makedirs(workdir)
        outfile = open(os.path.join(workdir, out_name), 'wb')
        self.outfiles[pk] = outfile
        try:
            proc = Popen(cmd, stdout=outfile, stderr=STDOUT,
                         cwd=workdir, env=envs, shell=False, bufsize=1,)
        except Exception as e:
            proc = FailedToStartProcess()
            logger.error(self.log_prefix(pk) + f"Popen error:\n{str(e)}\n")
            sleeptime = 0.5 + 3.5*random.random()
            time.sleep(sleeptime)
        self.processes[pk] = proc
    
    def _handle_error(self, pk, retcode):
        tail = self._log_error_tail(pk, retcode)

        if not self._can_retry(pk, retcode):
            self._cleanup_proc(pk)
            return (retcode, tail)
        else:
            self.outfiles[pk].close()
            self.start_times[pk] = time.time()
            self.retry_counts[pk] += 1
            self._launch_proc(pk)
            return 'running'

    def log_prefix(self, pk=None):
        prefix = f'rank {RANK} '
        if pk: prefix += f'{self.cuteids[pk]} '
        return prefix

    def write_message(self, job_statuses):
        msg = {'ask' : [], 'done' : [], 'error': []}
        for pk, status in job_statuses.items():
            if status == 'running':
                msg['ask'].append(pk)
            elif status == 'done':
                msg['done'].append(pk)
            else:
                retcode, tail = status
                msg['error'].append((pk, status[0], status[1]))
        return msg

    def update_processes(self):
        statuses = {}
        for pk, retcode in self._check_retcodes():
            if retcode is None:
                statuses[pk] = 'running'
            elif retcode == 0:
                statuses[pk] = 'done'
                self._cleanup_proc(pk)
            else:
                statuses[pk] = self._handle_error(pk, retcode)
        return statuses
    
    def exit(self):
        all_pks = list(self.processes.keys())
        for pk in all_pks:
            self._cleanup_proc(pk, timeout=self.CHECK_PERIOD)
        MPI.Finalize()
        sys.exit(0)

    def start_jobs(self, msg):
        assert msg['tag'] == 'NEW'
        for pk in msg:
            if pk == 'tag': continue
            job_spec = msg[pk]
            self.job_specs[pk] = job_spec
            self.cuteids[pk] = job_spec['cuteid']
            self.start_times[pk] = time.time()
            self.retry_counts[pk] = 1
            self._launch_proc(pk)

    def kill_jobs(self, kill_pks):
        for pk in kill_pks: self._cleanup_proc(pk, timeout=0)

    def main(self):
        tag = None
        gpus_per_node = None
        self.gpus_per_node = comm.bcast(gpus_per_node, root=0)
        while tag != 'EXIT':
            msg = comm.recv(source=0)
            tag = msg['tag']

            if tag == 'NEW':
                self.start_jobs(msg)
            elif tag == 'KILL':
                self.kill_jobs(msg['kill_pks'])
            elif tag == 'EXIT':
                logger.debug(f"rank {RANK} received EXIT")
                break

            statuses = self.update_processes()
            cuteids = ' '.join(self.cuteids.values())
            logger.debug(f"rank {RANK} jobs: {cuteids}")
            if len(statuses) > 0:
                msg = self.write_message(statuses)
                comm.send(msg, dest=0)
        self.exit()

if __name__ == "__main__":
    if RANK == 0:
        master = Master()
        def handle_term(signum, stack): master.EXIT_FLAG = True
        signal.signal(signal.SIGINT, handle_term)
        signal.signal(signal.SIGTERM, handle_term)
        master.main()
    else:
        worker = Worker()
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        worker.main()
