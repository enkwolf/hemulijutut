"""
Celery tasks for checking a user's answers to file upload, code input and code
replace exercises.
"""
# TODO: Implement tests for ranked code exercises and group code exercises.
from __future__ import absolute_import

from collections import namedtuple
from django.utils import translation
from django.contrib.auth.models import User

from reversion import revisions as reversion

import redis

import json

# Result generation dependencies
import prettydiff.difflib as difflib

# Test dependencies
import tempfile
import os
import random

# Stage dependencies
import base64

# Command dependencies
import time
import shlex
import resource
import subprocess

from celery import shared_task, chain

# The test data
#from courses.models import FileExerciseTest, FileExerciseTestStage,\
    #FileExerciseTestCommand, FileExerciseTestExpectedOutput,\
    #FileExerciseTestIncludeFile
#from courses.models import CodeInputExerciseAnswer # code input exercise models
#from courses.models import CodeReplaceExerciseAnswer # code replace exercise models
#from courses.models import # ranked code exercise models
#from courses.models import # group code exercise models

# The users' answers
#from courses.models import UserFileUploadExerciseAnswer,\
    #FileUploadExerciseReturnFile

from courses import models as cm
from courses import evaluation_sec as sec
from courses.evaluation_utils import *

# TODO: Improve by following the guidelines here:
#       - https://news.ycombinator.com/item?id=7909201

JSON_INCORRECT = 0
JSON_CORRECT = 1
JSON_INFO = 2
JSON_ERROR = 3

@shared_task(name="courses.run-fileexercise-tests", bind=True)
def run_tests(self, user_id, instance_id, exercise_id, answer_id, lang_code, revision):
    # TODO: Actually, just receive the relevant ids for fetching the Django
    #       models here instead of in the Django view.
    # http://celery.readthedocs.org/en/latest/userguide/tasks.html#database-transactions

    #def run_tests(tests, test_files, student_files, reference_files):
    # tests: the metadata for all the tests
    # test_files: the files that are integral for the tests
    # student_files: the files the student uploaded - these will be tested
    # reference_files: the reference files - these will be tested the same way
    #                  as the student's files

    # TODO: Check if any input generators are used. If yes, find a way to supply
    #       the same generated input to both the student's and reference codes.
    #       Some possibilities:
    #       - Generate a seed _here_ and pass it on (wastes CPU time?)
    #       - Generate the inputs _here_ and pass them on (best guess)
    #       - Generate the inputs during the code evaluation process and have
    #         the other code set depend on them (complicated to implement)
    # TODO: Input generator targets:
    #       - stdin
    #       - readable file
    #       - as command line parameters!
    #       - in env?
    translation.activate(lang_code)

    try:
        exercise_object = cm.FileUploadExercise.objects.get(id=exercise_id)

        if revision is not None:
            old_exercise_object = reversion.get_for_object(exercise_object).get(revision=revision).object_version.object
            exercise_object = old_exercise_object
    except cm.FileUploadExercise.DoesNotExist as e:
        # TODO: Log weird request
        return # TODO: Find a way to signal the failure to the user

    self.update_state(state="PROGRESS", meta={"current":4, "total":10})
    user_object = User.objects.get(id=user_id)
    print("user: %s" % (user_object.username))
    
    # Get the test data
    # TODO: Use prefetch_related?
    
    #tests = cm.FileExerciseTest.objects.filter(exercise=exercise_id)
    tests = exercise_object.fileexercisetest_set.all()

    student_results = {}
    reference_results = {}
    
    # Run all the tests for both the returned and reference code
    for i, test in enumerate(tests):
        self.update_state(state="PROGRESS", meta={"current": i, "total": len(tests)})

        # TODO: The student's code can be run in parallel with the reference
        results = run_test(test.id, answer_id, instance_id, exercise_id, student=True, revision=revision)
        student_results.update(results)
        results = run_test(test.id, answer_id, instance_id, exercise_id, revision=revision)
        reference_results.update(results)

    #print(student_results.items())
    #print(reference_results.items())

    results = {"student": student_results, "reference": reference_results}

    # TODO: Make the comparisons to determine correct status
    # Ultimate encoding: http://en.wikipedia.org/wiki/Code_page_437
    # Determine the result and generate JSON accordingly
    # TODO: Do this concurrently, interleaved with the actual test running!
    evaluation = generate_results(results, exercise_id)

    # Save the rendered results into Redis
    # TODO: How to access CELERY_RESULT_BACKEND in settings?
    task_id = self.request.id
    r = redis.StrictRedis(host='localhost', port=6379, db=0)
    r.set(task_id, json.dumps(evaluation))

    # Save the results to database
    result_string = json.dumps(results)
    correct = evaluation["correct"]
    points = exercise_object.default_points
    
    evaluation_obj = cm.Evaluation(test_results=result_string,
                                   points=points,
                                   correct=correct)
    evaluation_obj.save()
    
    try:
        answer_object = cm.UserFileUploadExerciseAnswer.objects.get(id=answer_id)
    except cm.UserFileUploadExerciseAnswer.DoesNotExist as e:
        # TODO: Log weird request
        return # TODO: Find a way to signal the failure to the user
    answer_object.evaluation = evaluation_obj
    answer_object.save()

    return evaluation_obj.id
    
    # TODO: Should we:
    # - return the results? (no)
    # - save the results directly into db? (is this worker contained enough?)
    # - send the results to a more privileged Celery worker for saving into db?

def generate_results(results, exercise_id):
    evaluation = {}
    correct = True

    student = results["student"]
    reference = results["reference"]

    # It's possible some of the tests weren't run at all
    unmatched = set(student.keys()) ^ set(reference.keys())
    matched = set(student.keys()) & set(reference.keys()) if unmatched else reference.keys()
    test_tree = {
        'tests': [],
        'messages': [],
        'errors': [],
        'hints': [],
        'triggers': [],
    }

    #### GO THROUGH ALL TESTS
    for test_id, student_t, reference_t in ((k, student[k], reference[k]) for k in matched):
        current_test = {
            "test_id": test_id,
            "name": student_t["name"],
            "correct": True,
            "stages": [],
        }

        test_tree["tests"].append(current_test)

        student_stages = student_t["stages"]
        reference_stages = reference_t["stages"]

        unmatched_stages = set(student_stages.keys()) ^ set(reference_stages.keys())
        matched_stages = set(student_stages.keys()) & set(reference_stages.keys())

        #### GO THROUGH ALL STAGES
        for stage_id, student_s, reference_s in ((k, student_stages[k], reference_stages[k])
                                                  for k in sorted(matched_stages,
                                                                  key=lambda x: student_stages[x]["ordinal_number"])):
            current_stage = {
                'stage_id': stage_id,
                'name': student_s['name'],
                'ordinal_number': student_s['ordinal_number'],
                'fail': student_s['fail'],
                'commands': [],
            }
            current_test['stages'].append(current_stage)

            student_cmds = student_s["commands"]
            reference_cmds = reference_s["commands"]

            #### GO THROUGH ALL COMMANDS
            for cmd_id, student_c, reference_c in ((k, student_cmds[k], reference_cmds[k])
                                                    for k in sorted(student_cmds.keys(),
                                                                    key=lambda x: student_cmds[x]["ordinal_number"])):
                cmd_correct = True if not student_c.get('fail') else False
                current_cmd = {
                    "cmd_id": cmd_id,
                }
                current_cmd.update(student_c)
                current_stage["commands"].append(current_cmd)

                # Handle stdin

                current_cmd["input_text"] = current_cmd["input_text"]

                # Handle JSON outputting testers

                if student_c['json_output']:
                    student_stdout = student_c["stdout"]
                    try:
                        json_results = json.loads(student_stdout)
                    except json.decoder.JSONDecodeError as e:
                        test_tree['errors'].append("JSONDecodeError: {}".format(str(e)))
                        print("Error decoding JSON output: {}".format(str(e)))
                    else:
                        tester = json_results.get('tester', "")
                        for test in json_results.get('tests', []):
                            test_title = test.get('title')
                            test_msg = {'title': test_title, 'msgs': []}
                            for test_run in test.get('runs', []):
                                for test_output in test_run.get('output', []):
                                    output_triggers = test_output.get('triggers', [])
                                    output_hints = test_output.get('hints', [])
                                    output_msg = test_output.get('msg', '')
                                    output_flag = test_output.get('flag', 0)

                                    test_tree['triggers'].extend(output_triggers)
                                    test_tree['hints'].extend(output_hints)
                                    test_msg['msgs'].append(output_msg)
                                    
                                    if output_flag == JSON_INCORRECT:
                                        cmd_correct = False
                                    if output_flag == JSON_ERROR:
                                        cmd_correct = False
                            test_tree['messages'].append(test_msg)

                    if student_c['stderr']:
                        test_tree['errors'].append(student_c['stderr'])
                else:
                    # Handle stdout

                    student_stdout = student_c["stdout"]
                    reference_stdout = reference_c["stdout"]

                    if student_c["significant_stdout"] and student_stdout != reference_stdout:
                        cmd_correct = False

                    if student_stdout or reference_stdout:
                        stdout_diff = difflib.HtmlDiff().make_table(
                            fromlines=student_stdout.splitlines(), tolines=reference_stdout.splitlines(),
                            fromdesc="Your program's output", todesc="Expected output"
                        )
                    else:
                        stdout_diff = ""
                    current_cmd["stdout_diff"] = stdout_diff

                    # Handle stderr

                    student_stderr = student_c["stderr"]
                    reference_stderr = reference_c["stderr"]

                    if student_c["significant_stderr"] and student_stderr != reference_stderr:
                        cmd_correct = False

                    if student_stderr or reference_stderr:
                        stderr_diff = difflib.HtmlDiff().make_table(
                            fromlines=student_stderr.splitlines(), tolines=reference_stderr.splitlines(),
                            fromdesc="Your program's errors", todesc="Expected errors"
                        )
                    else:
                        stderr_diff = ""
                    current_cmd["stderr_diff"] = stderr_diff

                current_test["correct"] = cmd_correct if current_test["correct"] else False
                if cmd_correct == False: correct = False
               

    evaluation.update({
        "correct": correct,
        "test_tree": test_tree,
    })
    return evaluation

@shared_task(name="courses.run-test", bind=True)
def run_test(self, test_id, answer_id, instance_id, exercise_id, student=False, revision=None):
    """
    Runs all the stages of the given test.
    """
    try:
        test = cm.FileExerciseTest.objects.get(id=test_id)

        if revision is not None:
            old_test = reversion.get_for_object(test).get(revision=revision).object_version.object
            test = old_test
    except cm.FileExerciseTest.DoesNotExist as e:
        # TODO: Log weird request
        return # TODO: Find a way to signal the failure to the user

    try:
        #stages = cm.FileExerciseTestStage.objects.filter(test=test_id)
        stages = test.fileexerciseteststage_set.all()
    except cm.FileExerciseTestStage.DoesNotExist as e:
        # TODO: Log weird request
        return # TODO: Find a way to signal the failure to the user

    try:
        # TODO: Fallback file names for those that don't have translations?
        exercise_file_objects = cm.FileExerciseTestIncludeFile.objects.filter(exercise=exercise_id)
        #exercise_file_objects = test.fileexercisetestincludefile_set.all()

        if revision is not None:
            old_exercise_file_objects = [
                reversion.get_for_object(exercise_file_object).get(revision=revision).object_version.object
                for exercise_file_object in exercise_file_objects
            ]
            exercise_file_objects = old_exercise_file_objects
    except cm.FileExerciseTestIncludeFile.DoesNotExist as e:
        # TODO: Log weird request
        return # TODO: Find a way to signal the failure to the user

    try:
        # TODO: Fallback file names for those that don't have translations?
        instance_file_links = cm.InstanceIncludeFileToExerciseLink.objects.filter(
            exercise=exercise_id, include_file__instance=instance_id,
        )

        if revision is not None:
            old_instance_file_links = [
                reversion.get_for_object(instance_file_link).get(revision=revision).object_version.object
                for instance_file_link in instance_file_links
                ]
            instance_file_links = old_instance_file_links
    except cm.InstanceIncludeFileToExerciseLink.DoesNotExist as e:
        # TODO: Log weird request
        return # TODO: Find a way to signal the failure to the user

    # Note: requires a shared/cloned file system!
    if student:
        try:
            answer_object = cm.UserFileUploadExerciseAnswer.objects.get(id=answer_id)
        except cm.UserFileUploadExerciseAnswer.DoesNotExist as e:
            # TODO: Log weird request
            return # TODO: Find a way to signal the failure to the user
        
        files_to_check = answer_object.get_returned_files_raw()
        print("".join("%s:\n%s" % (n, c) for n, c in files_to_check.items()))
    else:
        files_to_check = {f.file_settings.name: f.get_file_contents()
                          for f in exercise_file_objects
                          if f.file_settings.purpose == "REFERENCE"}
        print("".join("%s:\n%s" % (n, c) for n, c in files_to_check.items()))

    # TODO: Replace with the directory of the ramdisk
    temp_dir_prefix = os.path.join("/", "tmp")

    test_results = {test_id: {"fail": True, "name": test.name, "stages": {}}}
    with tempfile.TemporaryDirectory(dir=temp_dir_prefix) as test_dir:
        # Write the exercise files required by this test
        for name, contents in ((f.file_settings.name, f.get_file_contents())
                               for f in exercise_file_objects
                               if f in test.required_files.all() and
                               f.file_settings.purpose in ("INPUT", "WRAPPER", "TEST")):
            fpath = os.path.join(test_dir, name)
            with open(fpath, "wb") as fd:
                fd.write(contents)
            print("Wrote required exercise file {}".format(fpath))
            # TODO: chmod, chown, chgrp

        # Write the instance files required by this test
        for name, contents in ((fl.file_settings.name, fl.include_file.get_file_contents())
                               for fl in instance_file_links
                               if fl.include_file in test.required_instance_files.all() and
                               fl.file_settings.purpose in ('INPUT', 'WRAPPER', 'TEST')):
            fpath = os.path.join(test_dir, name)
            with open(fpath, 'wb') as fd:
                fd.write(contents)
            print("Wrote required instance file {}".format(fpath))
            # TODO: chmod, chown, chgrp

        # Write the files under test
        for name, contents in files_to_check.items():
            fpath = os.path.join(test_dir, name)
            with open(fpath, "wb") as fd:
                fd.write(contents)
            print("Wrote file under test %s" % (fpath))
            # TODO: chmod, chown, chgrp

        # TODO: Replace with chaining
        for i, stage in enumerate(stages):
            #self.update_state(state="PROGRESS",
                              #meta={"current": i, "total": len(stages)})
            if revision is not None:
                old_stage = reversion.get_for_object(stage).get(revision=revision).object_version.object
                stage = old_stage
            
            stage_results = run_stage(stage.id, test_dir, temp_dir_prefix,
                                      list(files_to_check.keys()), revision=revision)
            test_results[test_id]["stages"][stage.id] = stage_results
            test_results[test_id]["stages"][stage.id]["name"] = stage.name
            test_results[test_id]["stages"][stage.id]["ordinal_number"] = stage.ordinal_number

            if stage_results["fail"] == True:
                break

            # TODO: Read the directory and save the stage results in cache
            cache_results = False # TODO: Determine this by looking at dependencies
            if cache_results == True:
                test_dir_contents = os.listdir(test_dir)
                for fp in test_dir_contents:
                    with open(fp, "rb") as fd:
                        contents = fd.read()
        else:
            test_results[test_id]["fail"] = False

        # TODO: Read the expected output files (check the cache first?)
        test_dir_contents = os.listdir(test_dir)

    return test_results

@shared_task(name="courses.run-stage", bind=True)
def run_stage(self, stage_id, test_dir, temp_dir_prefix, files_to_check, revision=None):
    """
    Runs all the commands of this stage and collects the return values and the
    outputs.
    """

    try:
        commands = cm.FileExerciseTestCommand.objects.filter(stage=stage_id)
    except cm.FileExerciseTestCommand.DoesNotExist as e:
        # TODO: Log weird request
        return # TODO: Find a way to signal the failure to the user

    stage_results = {
        "fail": False,
        "commands": {},
    }

    if len(commands) == 0:
        return stage_results

    # TODO: Use this, but make appropriate changes elsewhere.
    """
    cmd_chain = chain(
        run_command_chainable.s(
            {"id":cmd.id, "input_text":cmd.input_text, "return_value":cmd.return_value},
            temp_dir_prefix, test_dir, files_to_check
        )
        for cmd in commands
    )

    try:
        results = cmd_chain().get()
    except:
        stage_results["fail"] = True
        results = {}
        print("exception at run_stage pokemon exception!")
    """
    # DEBUG #
    for i, cmd in enumerate(commands):
        if revision is not None:
            old_cmd = reversion.get_for_object(cmd).get(revision=revision).object_version.object
            cmd = old_cmd
        
        results = run_command_chainable(
            {"id":cmd.id, "input_text":cmd.input_text, "return_value":cmd.return_value},
            temp_dir_prefix, test_dir, files_to_check, stage_results=stage_results,
            revision=revision
        )
        stage_results.update(results)

        if results.get('fail'):
            stage_results['fail'] = True
            break

    # DEBUG #

    #stage_results.update(results)

    print(stage_results)

    return stage_results
    
    
    """# Old, blocking version
    for i, cmd in enumerate(commands):
        stage_results[cmd.id] = {}
        #self.update_state(state="PROGRESS",
                          #meta={"current-stage": i, "total-stage": len(commands)})
        
        # Create the outputs and inputs outside the test directory, thereby
        # effectively hiding them from unskilled users.
        stdout = tempfile.TemporaryFile(dir=temp_dir_prefix)
        stderr = tempfile.TemporaryFile(dir=temp_dir_prefix)
        stdin = tempfile.TemporaryFile(dir=temp_dir_prefix)
        stdin.write(bytearray(cmd.input_text, "utf-8"))

        proc_results = run_command(cmd.id, stdin, stdout, stderr, test_dir, files_to_check)
        stage_results[cmd.id].update(proc_results)

        stdout.seek(0)
        stage_results[cmd.id]["stdout"] = base64.standard_b64encode(stdout.read()).decode("ASCII")
        stdout.close()
        stderr.seek(0)
        stage_results[cmd.id]["stderr"] = base64.standard_b64encode(stderr.read()).decode("ASCII")
        stderr.close()

        # If the command failed, abort the stage
        if cmd.return_value is not None and cmd.return_value != proc_results["retval"]:
            break
        #if cmd.expected.stdout != stage_results[cmd.id]["stdout"]:
            #break
        #if cmd.expected.stderr != stage_results[cmd.id]["stderr"]:
            #break
    else:
        stage_results["fail"] = False

    return stage_results
    """

    # determine if the stage fails
    # if the stage fails, we can abort the tests/stages dependent on this stage
    # IDEA: stages are defined to be used by any test making those tests run or
    #       use the cached result of that stage
    #       - possible pitfall: same command, e.g. python student.py
    #         interpreted as "same stage"
    # IDEA: each stage can define a stage they depend on, making a directed
    #       graph of dependent stages


@shared_task(name="courses.run-command-chain-block")
def run_command_chainable(cmd, temp_dir_prefix, test_dir, files_to_check, stage_results=None, revision=None):
    cmd_id, cmd_input_text, cmd_return_value = cmd["id"], cmd["input_text"], cmd["return_value"]
    if stage_results is None or "commands" not in stage_results.keys():
        stage_results = {"commands": {}}

    stdout = tempfile.TemporaryFile(dir=temp_dir_prefix)
    stderr = tempfile.TemporaryFile(dir=temp_dir_prefix)
    stdin = tempfile.TemporaryFile(dir=temp_dir_prefix)
    stdin.write(bytearray(cmd_input_text, "utf-8"))
    stdin.seek(0)
    
    proc_results = run_command(cmd_id, stdin, stdout, stderr, test_dir, files_to_check, revision=revision)
    
    stdout.seek(0)
    #proc_results["stdout"] = base64.standard_b64encode(stdout.read()).decode("ASCII")
    read_stdout = stdout.read()
    try:
        proc_results["stdout"] = read_stdout.decode("utf-8")
        proc_results["binary_stdout"] = False
    except UnicodeDecodeError as e:
        proc_results["stdout"] = cp437_decoder(read_stdout)
        proc_results["binary_stdout"] = True
    stdout.close()

    stderr.seek(0)
    #proc_results["stderr"] = base64.standard_b64encode(stderr.read()).decode("ASCII")
    read_stderr = stderr.read()
    try:
        proc_results["stderr"] = read_stderr.decode("utf-8")
        proc_results["binary_stderr"] = False
    except UnicodeDecodeError as e:
        proc_results["stderr"] = cp437_decoder(read_stderr)
        proc_results["binary_stderr"] = True
    stderr.close()

    if proc_results.get('fail'):
        stage_results['fail'] = True

    # TODO: Use ordinal number istead of id?
    stage_results["commands"][cmd_id] = proc_results

    #if cmd_return_value is not None and cmd_return_value != proc_results["retval"]:
        #raise Exception()

    return stage_results

@shared_task(name="courses.run-command")
def run_command(cmd_id, stdin, stdout, stderr, test_dir, files_to_check, revision=None):
    """
    Runs the current command of this stage by automated fork & exec.
    """
    try:
        command = cm.FileExerciseTestCommand.objects.get(id=cmd_id)

        if revision is not None:
            old_command = reversion.get_for_object(command).get(revision=revision).object_version.object
            command = old_command
    except cm.FileExerciseTestCommand.DoesNotExist as e:
        # TODO: Log weird request
        return # TODO: Find a way to signal the failure to the user

    # TODO: More codes (e.g., $TRANSLATION)
    cmd = command.command_line.replace(
        "$RETURNABLES",
        " ".join(shlex.quote(f) for f in files_to_check)
    ).replace(
        "$CWD",
        test_dir
    )
    timeout = command.timeout.total_seconds()
    env = { # Remember that some information (like PATH) may come from other sources
        'PWD': test_dir,
        'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        'LC_CTYPE': 'en_US.UTF-8',
    }
    args = shlex.split(cmd)

    shell_like_cmd = " ".join(shlex.quote(arg) for arg in args)

    proc_results = {
        'ordinal_number': command.ordinal_number,
        'expected_retval': command.return_value,
        'input_text': command.input_text,
        'significant_stdout': command.significant_stdout,
        'significant_stderr': command.significant_stderr,
        'json_output': command.json_output,
        'command_line': shell_like_cmd,
    }
    print("Running: {cmdline}".format(cmdline=shell_like_cmd))

    # TODO
    # If additional resource limits have been provided, generate a customised
    # process demotion function. Otherwise, use the default.
    #demote_process = sec.get_demote_process_fun()
    demote_process = sec.default_demote_process

    start_rusage = resource.getrusage(resource.RUSAGE_CHILDREN)
    start_time = time.time()
    try:
        proc = subprocess.Popen(
            args=args, bufsize=-1, executable=None,
            stdin=stdin, stdout=stdout, stderr=stderr, # Standard fds
            preexec_fn=demote_process,                 # Demote before fork
            close_fds=True,                            # Don't inherit fds
            shell=False,                               # Don't run in shell
            cwd=env['PWD'], env=env,
            universal_newlines=False                   # Binary stdout
        )
    except (FileNotFoundError, PermissionError) as e:
        # In case the executable is not found or permission to run the
        # file didn't exist.
        
        # TODO: Use the proper way to deal with exceptions in Celery tasks
        proc_results.update({
            'retval': None,
            'timedout': False,
            'killed': False,
            'runtime': 0,
            'error': str(e),
            'fail': True,
        })
        return proc_results
    
    proc_retval = None
    proc_timedout = False
    proc_killed = False
    
    try:
        proc.wait(timeout=timeout)
        proc_runtime = time.time() - start_time
        proc_retval = proc.returncode
    except subprocess.TimeoutExpired:
        proc_runtime = time.time() - start_time
        proc_retval = None
        proc_timedout = True
        proc.terminate() # Try terminating the process nicely
        time.sleep(0.5)  # Grace period to allow the process to terminate
    
    # TODO: Clean up by halting all action (forking etc.) by the student's process
    # with SIGSTOP and by killing the frozen processes with SIGKILL
    #sec.secure_kill()
    #proc_killed = True

    proc_runtime = proc_runtime or (time.time() - start_time)
    proc_retval = proc_retval or proc.returncode
    # Collect statistics on CPU time consumed by the student's process
    # Consider implementing wait3 and wait4 for subprocess
    # http://stackoverflow.com/a/7050436/2096560
    end_rusage = resource.getrusage(resource.RUSAGE_CHILDREN)
    ru_utime = end_rusage.ru_utime - start_rusage.ru_utime
    ru_stime = end_rusage.ru_stime - start_rusage.ru_stime

    proc_results.update({
        'retval': proc_retval,
        'timedout': proc_timedout,
        'killed': proc_killed,
        'runtime': proc_runtime,
        'usermodetime': ru_utime,
        'kernelmodetime': ru_stime,
    })
    
    #stdout.seek(0)
    #print("\n".join(l.decode("utf-8") for l in stdout.readlines()))
    #stderr.seek(0)
    #print("\n".join(l.decode("utf-8") for l in stderr.readlines()))
    #time.sleep(15)

    return proc_results

# TODO: Subtask division:
#       - Run all tests:
#           * individual tests for the student's program
#           * chainable stages (e.g. save the results of compilation, and then
#             use these results for each of the tests)
#           * dependent stages (e.g. must pass compilation to even try running)
#       - Run all reference program tests
#           * stages that depend on student's test stage success (no need run
#             if student's code fails to compile etc.)
#           * cacheable reference tests results
#               o checkbox for marking whether the results _can_ be cached
#                 (e.g. using an input generator prevents reference result
#                  caching -> automatically disable checkbox)
#       - Calculate the diffs in between the tests
#           * correct/failed status for individual tests!

# TODO: Progress checking!
#       - which subtasks have been completed
#       - how many tests have failed, how many have been correct
#       - how many and what kind are left

# TODO: Celery worker status checking:
# http://stackoverflow.com/questions/8506914/detect-whether-celery-is-available-running
def get_celery_worker_status():
    ERROR_KEY = "errors"
    try:
        from celery.task.control import inspect
        insp = inspect()
        d = insp.stats()
        if not d:
            d = { ERROR_KEY: 'No running Celery workers were found.' }
    except IOError as e:
        from errno import errorcode
        msg = "Error connecting to the backend: " + str(e)
        if len(e.args) > 0 and errorcode.get(e.args[0]) == 'ECONNREFUSED':
            msg += ' Check that the RabbitMQ server is running.'
        d = { ERROR_KEY: msg }
    except ImportError as e:
        d = { ERROR_KEY: str(e)}
    return d
