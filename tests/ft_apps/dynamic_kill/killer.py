import balsam.launcher.dag as dag
import time

start = time.time()

while dag.BalsamJob.objects.filter(name="slow_job").count() == 0:
    time.sleep(2)
    if time.time() - start > 40:
        raise RuntimeError("the slow job never started")

slow_job = dag.BalsamJob.objects.get(name='slow_job')
dag.kill(slow_job)
