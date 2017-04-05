import xml.etree.cElementTree as et
from os import sep
import os

from collections import namedtuple

from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_ADDED, EVENT_JOB_REMOVED, \
    EVENT_SCHEDULER_START, EVENT_SCHEDULER_SHUTDOWN, EVENT_SCHEDULER_PAUSED, EVENT_SCHEDULER_RESUMED
from apscheduler.schedulers.gevent import GeventScheduler

from core.config import paths
from core import workflow as wf
from core.case import subscription
from core.case import callbacks
from core.helpers import locate_workflows_in_directory, construct_workflow_name_key, extract_workflow_name

from copy import deepcopy
import concurrent

NUM_PROCESSES = 5

_WorkflowKey = namedtuple('WorkflowKey', ['playbook', 'workflow'])


def initialize_threading():
    global pool
    global workflows

    workflows = []

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=NUM_PROCESSES)


def shutdown_pool():
    global pool
    global workflows

    for future in concurrent.futures.as_completed(workflows):
        future.result(timeout=3)
    pool.shutdown(wait=False)

    workflows = {}


def executeWorkflowWorker(workflow, start, subs):
    subscription.set_subscriptions(subs)
    workflow.execute(start=start)

    return "done"


class Controller(object):
    def __init__(self, name="defaultController", appPath=None):
        self.name = name
        self.workflows = {}
        self.load_all_workflows_from_directory(path=appPath)
        self.instances = {}
        self.tree = None

        self.scheduler = GeventScheduler()
        self.scheduler.add_listener(self.__scheduler_listener(),
                                    EVENT_SCHEDULER_START | EVENT_SCHEDULER_SHUTDOWN
                                    | EVENT_SCHEDULER_PAUSED | EVENT_SCHEDULER_RESUMED
                                    | EVENT_JOB_ADDED | EVENT_JOB_REMOVED
                                    | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        self.ancestry = [self.name]

    def load_workflow_from_file(self, path, workflow_name, name_override=None, playbook_override=None):
        self.tree = et.ElementTree(file=path)
        playbook_name = playbook_override if playbook_override else os.path.splitext(os.path.basename(path))[0]
        for workflow in self.tree.iter(tag="workflow"):
            current_workflow_name = workflow.get('name')
            if current_workflow_name == workflow_name:
                if name_override:
                    workflow_name = name_override
                name = construct_workflow_name_key(playbook_name, workflow_name)
                key = _WorkflowKey(playbook_name, workflow_name)
                self.workflows[key] = wf.Workflow(name=name,
                                                  xml=workflow,
                                                  parent_name=self.name,
                                                  playbook_name=playbook_name)
                break
        else:
            return False

        self.addChildWorkflows()
        self.addWorkflowScheduledJobs()
        return True

    def loadWorkflowsFromFile(self, path, name_override=None, playbook_override=None):
        self.tree = et.ElementTree(file=path)
        playbook_name = playbook_override if playbook_override else os.path.splitext(os.path.basename(path))[0]
        for workflow in self.tree.iter(tag='workflow'):
            workflow_name = name_override if name_override else workflow.get('name')
            name = construct_workflow_name_key(playbook_name, workflow_name)
            key = _WorkflowKey(playbook_name, workflow_name)
            self.workflows[key] = wf.Workflow(name=name,
                                              xml=workflow,
                                              parent_name=self.name,
                                              playbook_name=playbook_name)
        self.addChildWorkflows()
        self.addWorkflowScheduledJobs()

    def load_all_workflows_from_directory(self, path=paths.workflows_path):
        if not path:
            path = paths.workflows_path
        for workflow in locate_workflows_in_directory(path):
            self.loadWorkflowsFromFile(os.path.join(path, workflow))

    def addChildWorkflows(self):
        for workflow in self.workflows:
            playbook_name = workflow.playbook
            children = self.workflows[workflow].options.children
            for child in children:
                workflow_key = _WorkflowKey(playbook_name, extract_workflow_name(child, playbook_name=playbook_name))
                if workflow_key in self.workflows:
                    children[child] = self.workflows[workflow_key]

    def addWorkflowScheduledJobs(self):
        for workflow in self.workflows:
            if (self.workflows[workflow].options.enabled
                    and self.workflows[workflow].options.scheduler["autorun"] == "true"):
                schedule_type = self.workflows[workflow].options.scheduler["type"]
                schedule = self.workflows[workflow].options.scheduler["args"]
                self.scheduler.add_job(self.workflows[workflow].execute, trigger=schedule_type, replace_existing=True,
                                       **schedule)

    def create_workflow_from_template(self,
                                      playbook_name,
                                      workflow_name,
                                      template_playbook='emptyWorkflow',
                                      template_name='emptyWorkflow'):
        path = '{0}{1}{2}.workflow'.format(paths.templates_path, sep, template_playbook)
        return self.load_workflow_from_file(path=path,
                                            workflow_name=template_name,
                                            name_override=workflow_name,
                                            playbook_override=playbook_name)

    def create_playbook_from_template(self, playbook_name,
                                      template_playbook='emptyWorkflow'):
        # TODO: Need a handler for returning workflow key and status
        path = '{0}{1}{2}.workflow'.format(paths.templates_path, sep, template_playbook)
        self.loadWorkflowsFromFile(path=path, playbook_override=playbook_name)

    def removeWorkflow(self, playbook_name, workflow_name):
        name = _WorkflowKey(playbook_name, workflow_name)
        if name in self.workflows:
            del self.workflows[name]
            return True
        return False

    def remove_playbook(self, playbook_name):
        for name in [workflow for workflow in self.workflows if workflow.playbook == playbook_name]:
            del self.workflows[name]
            return True
        return False

    def get_all_workflows(self):
        result = {}
        for key in self.workflows.keys():
            if key.playbook not in result:
                result[key.playbook] = []
            result[key.playbook].append(key.workflow)
        return result

    def is_workflow_registered(self, playbook_name, workflow_name):
        return _WorkflowKey(playbook_name, workflow_name) in self.workflows

    def is_playbook_registerd(self, playbook_name):
        return any(workflow_key.playbook == playbook_name for workflow_key in self.workflows)

    def update_workflow_name(self, old_playbook, old_workflow, new_playbook, new_workflow):
        old_key = _WorkflowKey(old_playbook, old_workflow)
        new_key = _WorkflowKey(new_playbook, new_workflow)
        self.workflows[new_key] = self.workflows.pop(old_key)
        self.workflows[new_key].name = construct_workflow_name_key(new_playbook, new_workflow)

    def update_playbook_name(self, old_playbook, new_playbook):
        for key in [name for name in self.workflows.keys() if name.playbook == old_playbook]:
            self.update_workflow_name(old_playbook, key.workflow, new_playbook, key.workflow)

    def executeWorkflow(self, playbook_name, workflow_name, start="start"):
        global pool
        global workflows

        key = _WorkflowKey(playbook_name, workflow_name)
        workflow = self.workflows[key]
        subs = deepcopy(subscription.subscriptions)

        workflows.append(pool.submit(executeWorkflowWorker, workflow, start, subs))

        callbacks.SchedulerJobExecuted.send(self)

    def get_workflow(self, playbook_name, workflow_name):
        key = _WorkflowKey(playbook_name, workflow_name)
        if key in self.workflows:
            return self.workflows[key]
        return None

    def playbook_to_xml(self, playbook_name):
        workflows = [workflow for key, workflow in self.workflows.items() if key.playbook == playbook_name]
        if workflows:
            xml = et.Element("workflows")
            for workflow in workflows:
                xml.append(workflow.to_xml())
            return xml
        else:
            return None

    # Starts active execution
    def start(self):
        self.scheduler.start()
        return self.scheduler.state

    # Stops active execution
    def stop(self, wait=True):
        self.scheduler.shutdown(wait=wait)
        return self.scheduler.state

    # Pauses active execution
    def pause(self):
        self.scheduler.pause()
        return self.scheduler.state

    # Resumes active execution
    def resume(self):
        self.scheduler.resume()
        return self.scheduler.state

    # Pauses active execution of specific job
    def pauseJob(self, job_id):
        self.scheduler.pause_job(job_id=job_id)

    # Resumes active execution of specific job
    def resumeJob(self, job_id):
        self.scheduler.resume_job(job_id=job_id)

    # Returns jobs scheduled for active execution
    def getScheduledJobs(self):
        self.scheduler.get_jobs()

    def __scheduler_listener(self):
        event_selector_map = {EVENT_SCHEDULER_START: (lambda: callbacks.SchedulerStart.send(self)),
                              EVENT_SCHEDULER_SHUTDOWN: (lambda: callbacks.SchedulerShutdown.send(self)),
                              EVENT_SCHEDULER_PAUSED: (lambda: callbacks.SchedulerPaused.send(self)),
                              EVENT_SCHEDULER_RESUMED: (lambda: callbacks.SchedulerResumed.send(self)),
                              EVENT_JOB_ADDED: (lambda: callbacks.SchedulerJobAdded.send(self)),
                              EVENT_JOB_REMOVED: (lambda: callbacks.SchedulerJobRemoved.send(self)),
                              EVENT_JOB_EXECUTED: (lambda: callbacks.SchedulerJobExecuted.send(self)),
                              EVENT_JOB_ERROR: (lambda: callbacks.SchedulerJobError.send(self))}

        def event_selector(event):
            try:
                event_selector_map[event.code]()
            except KeyError:
                print("Error: Unknown event sent!")

        return event_selector


controller = Controller()
