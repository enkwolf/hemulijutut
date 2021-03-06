"""
Django views for rendering the course contents and checking exercises.
"""
import datetime
import json
from cgi import escape
from collections import namedtuple

from django.http import HttpResponse, JsonResponse, HttpResponseRedirect,\
    HttpResponseNotFound, HttpResponseForbidden, HttpResponseNotAllowed,\
    HttpResponseServerError
from django.db.models import Q
from django.template import Template, loader, engines
from django.core.urlresolvers import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext as _
from django.contrib import messages

from reversion import revisions as reversion

from lovelace.celery import app as celery_app
import courses.tasks as rpc_tasks
from courses.tasks import get_celery_worker_status

from courses.models import *
from courses.forms import *
from feedback.models import *

import courses.markupparser as markupparser
import courses.blockparser as blockparser

def is_course_staff(user, instance):
    if user.is_superuser:
        return True
    elif user.is_staff:
        return user in instance.course.staff_group.user_set.get_queryset()
    else:
        return False
    

def cookie_law(view_func):
    """
    To comply with the European Union cookie law, display a warning about the
    site using cookies. When the user accepts cookies, set a session variable to
    disable the message.
    """
    def func_wrapper(request, *args, **kwargs):
        if "cookies_accepted" in request.COOKIES:
            if request.COOKIES["cookies_accepted"] == "1":
                request.session["cookies_accepted"] = True
        if request.session.get("cookies_accepted"):
            pass
        else:
            request.session["cookies_accepted"] = False
        return view_func(request, *args, **kwargs)
    return func_wrapper

@cookie_law
def index(request):
    course_list = Course.objects.all()

    t = loader.get_template("courses/index.html")
    c = {
        'course_list': course_list,
    }
    return HttpResponse(t.render(c, request))

@cookie_law
def course_instances(request, course_slug):
    return HttpResponse("here be instances for this course")

# TODO: A tool for locking the current revisions of embedded pages for
# course instances.

@cookie_law
def course(request, course_slug, instance_slug):
    try:
        course_obj = Course.objects.get(slug=course_slug)
    except Course.DoesNotExist:
        return HttpResponseNotFound("Course {} does not exist!".format(course_slug))

    try:
        instance_obj = CourseInstance.objects.get(slug=instance_slug)
    except CourseInstance.DoesNotExist:
        return HttpResponseNotFound("Course {} does not have an instance {}!".
                                    format(course_obj.name, instance_slug))

    frontpage = instance_obj.frontpage
    if frontpage:
        content_slug = frontpage.slug
        context = content(request, course_slug, instance_slug, content_slug, frontpage=True)
    else:
        context = {}

    context["course"] = course_obj
    context["instance"] = instance_obj

    if is_course_staff(request.user, instance_obj):
        contents = instance_obj.contents.all().order_by('ordinal_number')
    else:
        contents = instance_obj.contents.filter(visible=True).order_by('ordinal_number')
    
    if len(contents) > 0:
        tree = []
        tree.append((mark_safe('>'), None, None, None, None))
        for content_ in contents:
            course_tree(tree, content_, request.user, instance_obj)
        tree.append((mark_safe('<'), None, None, None, None))
        context["content_tree"] = tree

    t = loader.get_template("courses/course.html")
    return HttpResponse(t.render(context, request))

def course_tree(tree, node, user, instance_obj):
    embedded_links = EmbeddedLink.objects.filter(parent=node.content.id)
    embedded_count = len(embedded_links)
    correct_embedded = 0
    
    evaluation = ""
    if user.is_authenticated():
        exercise = node.content
        evaluation = exercise.get_user_evaluation(exercise, user)

        if embedded_count > 0:
            for emb_exercise in embedded_links.values_list('embedded_page', flat=True):
                emb_exercise = ContentPage.objects.get(id=emb_exercise)
                print(emb_exercise.name)
                correct_embedded += 1 if emb_exercise.get_user_evaluation(emb_exercise, user) == "correct" else 0
    
    list_item = (node.content, evaluation, correct_embedded, embedded_count, node.visible)
    
    if list_item not in tree:
        tree.append(list_item)

    if is_course_staff(user, instance_obj):
        children = ContentGraph.objects.filter(parentnode=node).order_by('ordinal_number')
    else:
        children = ContentGraph.objects.filter(parentnode=node, visible=True).order_by('ordinal_number')
    
    if len(children) > 0:
        tree.append((mark_safe('>'), None, None, None, None))
        for child in children:
            course_tree(tree, child, user, instance_obj)
        tree.append((mark_safe('<'), None, None, None, None))

def check_answer_sandboxed(request, content_slug):
    """
    Saves and evaluates a user's answer to an exercise and sends the results
    back to the user in sanboxed mode.
    """

    course_slug = "sandbox"
    
    if request.method != "POST":
        return HttpResponseNotAllowed(['POST'])

    user = request.user
    if not user.is_authenticated() or not user.is_active or not user.is_staff:
        return JsonResponse({
            'result': 'Only logged in admins can send their answers for evaluation!'
        })

    content = ContentPage.objects.get(slug=content_slug)
    
    user = request.user
    ip = request.META.get('REMOTE_ADDR')
    answer = request.POST
    files = request.FILES

    exercise = content.get_type_object()
    
    try:
        answer_object = exercise.save_answer(user, ip, answer, files)
    except InvalidExerciseAnswerException as e:
        return JsonResponse({
            'result': str(e)
        })
    evaluation = exercise.check_answer(user, ip, answer, files, answer_object)
    if not exercise.manually_evaluated:
        if exercise.content_type == "FILE_UPLOAD_EXERCISE":
            task_id = evaluation["task_id"]
            return check_progress(request, None, content_slug, task_id)
        exercise.save_evaluation(user, evaluation, answer_object)
        evaluation["manual"] = False
    else:
        evaluation["manual"] = True

    # TODO: Errors, hints, comments in JSON
    t = loader.get_template("courses/exercise-evaluation.html")
    return JsonResponse({
        'result': t.render(evaluation),
        'evaluation': evaluation.get("evaluation"),
    })

def check_answer(request, course_slug, instance_slug, content_slug, revision):
    """
    Saves and evaluates a user's answer to an exercise and sends the results
    back to the user.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(['POST'])

    if not request.user.is_active:
        return JsonResponse({
            'result': 'Only logged in users can send their answers for evaluation!'
        })

    course = Course.objects.get(slug=course_slug)
    instance = CourseInstance.objects.get(slug=instance_slug)
    content = ContentPage.objects.get(slug=content_slug)
    # TODO: Ensure that the content revision really belongs to the course instance

    # TODO: The answer has to be linked to the revision the user is answering!

    # TODO: Resolve "head" revision to the newest revision
    if revision == "head":
        #reversion.
        pass

    # Check if a deadline exists and if it has already passed
    try:
        content_graph = instance.contents.filter(content=content).first()
    except ContentGraph.DoesNotExist as e:
        pass
    else:
        if content_graph is not None:
            if not content_graph.deadline or datetime.datetime.now() < content_graph.deadline:
                pass
            else:
                # TODO: Use a template!
                return JsonResponse({
                    'result': 'The deadline for this exercise (%s) has passed. Your answer will not be evaluated!' % (content_graph.deadline)
                })

    user = request.user
    ip = request.META.get('REMOTE_ADDR')
    answer = request.POST
    files = request.FILES

    exercise = content
    
    try:
        answer_object = exercise.save_answer(content, user, ip, answer, files, instance, revision)
    except InvalidExerciseAnswerException as e:
        return JsonResponse({
            'result': str(e)
        })
    evaluation = exercise.check_answer(content, user, ip, answer, files, answer_object, revision)
    if not exercise.manually_evaluated:
        if exercise.content_type == "FILE_UPLOAD_EXERCISE":
            task_id = evaluation["task_id"]
            return check_progress(request, course_slug, instance_slug, content_slug, revision, task_id)
        exercise.save_evaluation(content, user, evaluation, answer_object)
        evaluation["manual"] = False
    else:
        evaluation["manual"] = True

    msg_context = {
        'course_slug': course_slug,
        'instance_slug': instance_slug,
    }
    hints = ["".join(markupparser.MarkupParser.parse(msg, request, msg_context)).strip()
             for msg in evaluation.get('hints', [])]

    answer_count = exercise.get_user_answers(exercise, user).count()
    answer_count_str = get_answer_count_meta(answer_count)

    # TODO: Errors, hints, comments in JSON
    t = loader.get_template("courses/exercise-evaluation.html")
    print(evaluation)
    return JsonResponse({
        'result': t.render(evaluation),
        'hints': hints,
        'evaluation': evaluation.get("evaluation"),
        'answer_count_str': answer_count_str,
    })

def check_progress(request, course_slug, instance_slug, content_slug, revision, task_id):
    # Based on https://djangosnippets.org/snippets/2898/
    # TODO: Check permissions
    task = celery_app.AsyncResult(id=task_id)
    info = task.info
    if task.ready():
        return file_exercise_evaluation(request, course_slug, instance_slug, content_slug, revision, task_id, task)
    else:
        celery_status = get_celery_worker_status()
        if "errors" in celery_status:
            data = celery_status
        else:
            progress_url = reverse('courses:check_progress',
                                   kwargs={'course_slug': course_slug,
                                           'instance_slug': instance_slug,
                                           'content_slug': content_slug,
                                           'revision': revision,
                                           'task_id': task_id,})
            if not info: info = task.info # Try again in case the first time was too early
            data = {"state": task.state, "metadata": info, "redirect": progress_url}
        return JsonResponse(data)

def get_answer_count_meta(answer_count):
    # TODO: Maybe refactor
    t = engines['django'].from_string("""{% load i18n %}{% blocktrans count counter=answer_count %}<span class="answer-count">{{ counter }}</span> answer{% plural %}<span class="answer-count">{{ counter }}</span> answers{% endblocktrans %}""")
    return t.render({'answer_count': answer_count})
    
    
def file_exercise_evaluation(request, course_slug, instance_slug, content_slug, revision, task_id, task=None):
    if task is None:
        task = AsyncResult(task_id)
    evaluation_id = task.get()
    task.forget() # TODO: IMPORTANT! Also forget all the subtask results somehow? in tasks.py?
    evaluation_obj = Evaluation.objects.get(id=evaluation_id)
    content = ContentPage.objects.get(slug=content_slug)
    answer_count = content.get_user_answers(content, request.user).count()
    answer_count_str = get_answer_count_meta(answer_count)

    # TODO: Nicer way to get the proper address!
    import redis
    r = redis.StrictRedis(host='localhost', port=6379, db=0)
    evaluation_json = r.get(task_id).decode("utf-8")
    evaluation_tree = json.loads(evaluation_json)
    r.delete(task_id)

    msg_context = {
        'course_slug': course_slug,
        'instance_slug': instance_slug,
    }
    messages = [
        (msg['title'], [
            "".join(markupparser.MarkupParser.parse(msg_msg, request, msg_context)).strip()
            for msg_msg in msg['msgs']
        ])
        for msg in evaluation_tree['test_tree'].get('messages', [])
    ]
    hints = ["".join(markupparser.MarkupParser.parse(msg, request, msg_context)).strip()
             for msg in evaluation_tree['test_tree'].get('hints', [])]
    triggers = evaluation_tree['test_tree'].get('triggers', [])

    debug_json = json.dumps(evaluation_tree, indent=4)

    t_file = loader.get_template("courses/file-exercise-evaluation.html")
    c_file = {
        'debug_json': debug_json,
        'evaluation_tree': evaluation_tree["test_tree"],
    }
    t_exercise = loader.get_template("courses/exercise-evaluation.html")
    c_exercise = {
        'evaluation': evaluation_obj.correct,
    }
    t_messages = loader.get_template('courses/exercise-evaluation-messages.html')
    data = {
        'file_tabs': t_file.render(c_file, request),
        'result': t_exercise.render(c_exercise),
        'evaluation': evaluation_obj.correct,
        'points': evaluation_obj.points,
        'messages': t_messages.render({'messages': messages}),
        'hints': hints,
        'triggers': triggers,
        'answer_count_str': answer_count_str,
    }
    return JsonResponse(data)

def get_old_file_exercise_evaluation(request, user, answer_id):
    if request.user.is_authenticated() and (request.user.username == user or request.user.is_staff):
        pass
    else:
        return HttpResponseForbidden("You're only allowed to view your own answers.")

    try:
        user_obj = User.objects.get(username=user)
    except User.DoesNotExist as e:
        return HttpResponseNotFound("No such user %s" % user)

    try:
        answer_obj = UserFileUploadExerciseAnswer.objects.get(id=answer_id)
    except UserFileUploadExerciseAnswer.DoesNotExist as e:
        return HttpResponseNotFound("No such answer {}".format(answer_id))
    else:
        if answer_obj.user != request.user and not is_course_staff(request.user, answer_obj.instance):
            return HttpResponseForbidden("You're only allowed to view your own answers.")

    from .tasks import generate_results
    
    results_json = answer_obj.evaluation.test_results
    results_dict = json.loads(results_json)

    evaluation_dict = generate_results(results_dict, 0)

    debug_json = json.dumps(evaluation_dict, indent=4)

    t_file = loader.get_template("courses/file-exercise-evaluation.html")
    c_file = {
        'debug_json': debug_json,
        'evaluation_tree': evaluation_dict["test_tree"],
    }
    return HttpResponse(t_file.render(c_file, request))

@cookie_law
def sandboxed_content(request, content_slug, **kwargs):
    try:
        content = ContentPage.objects.get(slug=content_slug)
    except ContentPage.DoesNotExist:
        return HttpResponseNotFound("Content {} does not exist!".format(content_slug))

    user = request.user
    if not user.is_authenticated() or not user.is_active or not user.is_staff:
        return HttpResponseForbidden("Only logged in admins can view pages in sandbox!")

    content_type = content.content_type
    question = blockparser.parseblock(escape(content.question))
    choices = answers = content.get_choices(content)

    rendered_content = content.rendered_markup(request)

    c = {
        'content': content,
        'content_name': content.name,
        'content_type': content_type,
        'rendered_content': rendered_content,
        'embedded': False,
        'question': question,
        'choices': choices,
        'user': user,
        'sandboxed': True,
    }
    if "frontpage" in kwargs:
        return c
    else:
        t = loader.get_template("courses/contentpage.html")
        return HttpResponse(t.render(c, request))

@cookie_law
def content(request, course_slug, instance_slug, content_slug, **kwargs):
    try:
        course = Course.objects.get(slug=course_slug)
    except Course.DoesNotExist:
        return HttpResponseNotFound("Course {} does not exist!".format(course_slug))

    try:
        instance = CourseInstance.objects.get(slug=instance_slug)
    except CourseInstance.DoesNotExist:
        return HttpResponseNotFound("Course {} does not have an instance {}!".
                                    format(course.name, instance_slug))
    
    try:
        content = ContentPage.objects.get(slug=content_slug)
    except ContentPage.DoesNotExist:
        return HttpResponseNotFound("Content {} does not exist!".format(content_slug))

    content_graph = None
    revision = None
    if "frontpage" not in kwargs:
        try:
            content_graph = instance.contents.filter(content=content).first()
        except ContentGraph.DoesNotExist:
            return HttpResponseNotFound("Content {} is not linked to course {}!".format(content_slug, course_slug))
        else:
            if content_graph is None:
                return HttpResponseNotFound("Content {} is not linked to course {}!".format(content_slug, course_slug))
        
        revision = content_graph.revision
    
    content_type = content.content_type

    context = {
        'course': course,
        'course_slug': course_slug,
        'instance': instance,
        'instance_slug': instance.slug,
    }

    term_context = context.copy()
    term_context['tooltip'] = True
    terms = Term.objects.filter(instance__course=course).exclude(Q(description__isnull=True) | Q(description__exact='')).order_by('name')
    term_div_data = []
    termbank_contents = {}
    for term in terms:
        slug = slugify(term.name, allow_unicode=True)
        term_div_data.append({
            'slug' : slug,
            'description' : "".join(markupparser.MarkupParser.parse(term.description, request, term_context)).strip(),
        })

        term_data = {
            'slug' : slug,
            'name' : term.name, 
            'span_id' : slug + '-termbank-span',
        }
        try:
            first_char = term.name.upper()[0]
        except IndexError:
            first_char = "#"
        else:
            if not first_char.isalpha():
                first_char = "#"
        if first_char in termbank_contents:
            termbank_contents[first_char].append(term_data)
        else:
            termbank_contents[first_char] = [term_data]
            
    rendered_content = ""

    # TODO: Admin link should point to the correct version!

    # TODO: Warn admins if the displayed version is not the current version!

    # Get the other things based on the rev. if set
    if revision is not None:
        # This seems unoptimal. Maybe create a patch to django-reversions?
        # reversion.get_revision_for_object or sth. would be nice...
        #version_list = reversion.get_for_object(content).order_by('revision_id')
        version = reversion.get_for_object(content).get(revision=revision).field_dict
        old_content = version["content"]
        question = version["question"]
        
        # Render the old version of the page
        markup_gen = markupparser.MarkupParser.parse(old_content, request, context)
        for chunk in markup_gen:
            rendered_content += chunk
    else:
        question = blockparser.parseblock(escape(content.question), {"course": course})

    choices = answers = content.get_choices(content, revision=revision)

    evaluation = None
    answer_count = None
    if request.user.is_authenticated():
        if request.user.is_active and content.is_answerable() and content.get_user_answers(content, request.user):
            answer_count = content.get_user_answers(content, request.user).count()
        if content_graph and (content_graph.publish_date is None or content_graph.publish_date < datetime.datetime.now()):
            try:
                evaluation = content.get_user_evaluation(content, request.user)
            except NotImplementedError:
                evaluation = None

    if not rendered_content:
        rendered_content = content.rendered_markup(request, context)

    c = {
        'course_slug': course_slug,
        'course_name': course.name,
        'instance_name': instance.name,
        'instance_slug': instance_slug,
        'content': content,
        'rendered_content': rendered_content,
        'embedded': False,
        'content_name': content.name,
        'content_type': content_type,
        'question': question,
        'choices': choices,
        'evaluation': evaluation,
        'answer_count': answer_count,
        'sandboxed': False,
        'termbank_contents': sorted(list(termbank_contents.items())),
        'term_div_data': term_div_data,
        'revision': revision,
    }
    if "frontpage" in kwargs:
        return c
    else:
        t = loader.get_template("courses/contentpage.html")
        return HttpResponse(t.render(c, request))

def user_profile_save(request):
    """
    Save the submitted form.
    """
    if not request.user.is_authenticated():
        return HttpResponseNotFound()
    if not request.method == "POST":
        return HttpResponseNotFound()
    form = request.POST
    if not set(["first_name", "last_name", "student_id", "study_program"]).issubset(form.keys()):
        return HttpResponseNotFound()

    profile = UserProfile.objects.get(user=request.user)

    request.user.first_name = form["first_name"][:30]
    request.user.last_name = form["last_name"][:30]
    try:
        profile.student_id = int(form["student_id"])
    except ValueError:
        return HttpResponseNotFound()
    profile.study_program = form["study_program"][:80]

    profile.save()
    request.user.save()
    return HttpResponseRedirect('/')

def user_profile(request):
    """
    Allow the user to change information in their profile.
    """
    if not request.user.is_authenticated():
        return HttpResponseNotFound()

    profile = UserProfile.objects.get(user=request.user)

    t = loader.get_template("courses/userprofile.html")
    c = {
        'username': request.user.username,
        'first_name': request.user.first_name,
        'last_name': request.user.last_name,
        'email': request.user.email,
        'student_id': profile.student_id,
        'study_program': profile.study_program,
    }
    return HttpResponse(t.render(c, request))

def user(request, user_name):
    '''Shows user information to the requesting user. The amount of information depends on who the
    requesting user is.'''
    if request.user == "AnonymousUser":
        # TODO: Don't allow anons to view anything
        # Return 404 even if the users exists to prevent snooping around
        # Distinguishing 403 and 404 here would give away information
        return HttpResponseNotFound()
    elif request.user == user_name:
        # TODO: Allow the user to view their own info and edit some of it
        pass
    elif request.user == "mdf":
        # TODO: Allow admins to view useful information regarding the user they've requested
        pass
    else:
        # TODO: Allow normal users to view some very basic information?
        pass
    
    checkboxexercise_answers = UserCheckboxExerciseAnswer.objects.filter(user=request.user)
    multiplechoiceexercise_answers = UserMultipleChoiceExerciseAnswer.objects.filter(user=request.user)
    textfieldexercise_answers = UserTextfieldExerciseAnswer.objects.filter(user=request.user)
    fileexercise_answers = UserFileUploadExerciseAnswer.objects.filter(user=request.user)

    t = loader.get_template("courses/userinfo.html")
    c = {
        'checkboxexercise_answers': checkboxexercise_answers,
        'multiplechoiceexercise_answers': multiplechoiceexercise_answers,
        'textfieldexercise_answers': textfieldexercise_answers,
        'fileexercise_answers': fileexercise_answers,
    }
    return HttpResponse(t.render(c, request))

def calendar_post(request, calendar_id, event_id):
    if not request.user.is_authenticated():
        return HttpResponseNotFound()
    if not request.method == "POST":
        return HttpResponseNotFound()
    form = request.POST

    event_id = int(event_id)

    try:
        calendar_date = CalendarDate.objects.get(id=event_id)
    except CalendarDate.DoesNotExist:
        return HttpResponseNotFound("Error: the selected calendar does not exist.")

    if "reserve" in form.keys() and int(form["reserve"]) == 1:
        # TODO: How to make this atomic?
        reservations = CalendarReservation.objects.filter(calendar_date_id=event_id)
        if reservations.count() >= calendar_date.reservable_slots:
            return HttpResponse("This event is already full.")
        user_reservations = reservations.filter(user=request.user)
        if user_reservations.count() >= 1:
            return HttpResponse("You have already reserved a slot in this event.")

        new_reservation = CalendarReservation(calendar_date_id=event_id, user=request.user)
        new_reservation.save()
        # TODO: Check that we didn't overfill the event
        return HttpResponse("Slot reserved!")
    elif "reserve" in form.keys() and int(form["reserve"]) == 0:
        user_reservations = CalendarReservation.objects.filter(calendar_date_id=event_id, user=request.user)
        if user_reservations.count() >= 1:
            user_reservations.delete()
            return HttpResponse("Reservation cancelled.")
        else:
            return HttpResponse("Reservation already cancelled.")
    else:
        return HttpResponseForbidden()

def show_answers(request, user, course, instance, exercise):
    """
    Show the user's answers for a specific exercise on a specific course.
    """
    try:
        instance_obj = CourseInstance.objects.get(slug=instance)
    except CourseInstance.DoesNotExist as e:
        return HttpResponseNotFound("No such course instance {}.".format(instance))
    
    if request.user.is_authenticated() and (request.user.username == user or is_course_staff(request.user, instance_obj)):
        pass
    else:
        return HttpResponseForbidden("You're only allowed to view your own answers.")
    
    try:
        user_obj = User.objects.get(username=user)
    except User.DoesNotExist as e:
        return HttpResponseNotFound("No such user {}.".format(user))
    
    try:
        course_obj = Course.objects.get(slug=course)
    except Course.DoesNotExist as e:
        return HttpResponseNotFound("No such course {}.".format(course))
    
    try:
        exercise_obj = ContentPage.objects.get(slug=exercise).get_type_object()
    except ContentPage.DoesNotExist as e:
        return HttpResponseNotFound("No such exercise {}.".format(exercise))
    
    content_type = exercise_obj.content_type
    question = exercise_obj.question
    choices = exercise_obj.get_choices(exercise_obj)
    
    # TODO: Error checking for exercises that don't belong to this course
    
    answers = []
    
    if content_type == "MULTIPLE_CHOICE_EXERCISE":
        answers = UserMultipleChoiceExerciseAnswer.objects.filter(user=user_obj, exercise=exercise_obj)
    elif content_type == "CHECKBOX_EXERCISE":
        answers = UserCheckboxExerciseAnswer.objects.filter(user=user_obj, exercise=exercise_obj)
    elif content_type == "TEXTFIELD_EXERCISE":
        answers = UserTextfieldExerciseAnswer.objects.filter(user=user_obj, exercise=exercise_obj)
    elif content_type == "FILE_UPLOAD_EXERCISE":
        answers = UserFileUploadExerciseAnswer.objects.filter(user=user_obj, exercise=exercise_obj)
    elif content_type == "CODE_REPLACE_EXERCISE":
        answers = UserCodeReplaceExerciseAnswer.objects.filter(user=user_obj, exercise=exercise_obj)
    
    answers = answers.order_by('-answer_date')
    
    # TODO: Own subtemplates for each of the exercise types.
    t = loader.get_template("courses/user-exercise-answers.html")
    c = {
        'exercise': exercise,
        'exercise_name': exercise_obj.name,
        'course_slug': course,
        'course_name': course_obj.name,
        'instance_slug': instance,
        'instance_name': instance_obj.name,
        'instance_email': instance_obj.email,
        'answers_url': request.build_absolute_uri(),
        'answers': answers,
        'username': user,
    }
    return HttpResponse(t.render(c, request))

def help_list(request):
    return HttpResponse()

def markup_help(request):
    markups = markupparser.MarkupParser.get_markups()
    Markup = namedtuple(
        'Markup',
        ['name', 'description', 'example', 'result', 'slug']
    )

    # For debugging
    #for _, m in sorted(markups.items()):
        #print("name: " + m.name)
        #print("result: " + "".join(markupparser.MarkupParser.parse(m.example)))
    
    markup_list = (
        Markup(m.name, m.description, m.example, mark_safe("".join(markupparser.MarkupParser.parse(m.example))),
               slugify(m.name, allow_unicode=True))
        for _, m in markups.items()
    )
    
    t = loader.get_template("courses/markup-help.html")
    c = {
        'markups': list(sorted(markup_list)),
    }
    return HttpResponse(t.render(c, request))

def terms(request):
    t = loader.get_template("courses/terms.html")
    c = {}
    return HttpResponse(t.render(c, request))

