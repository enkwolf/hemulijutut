from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save

# http://stackoverflow.com/questions/44109/extending-the-user-model-with-custom-fields-in-django
#class UserProfile(models.Model):
#    """The user profile."""
#    user = models.OneToOneField(User)
#    student_id = models.IntegerField()
#
#    def __unicode__(self):
#        return "%s's profile" % self.user

#def create_user_profile(sender, instance, created, **kwargs):
#    if created:
#        profile, created = UserProfile.objects.get_or_create(user=instance)

#post_save.connect(create_user_profile, sender=User)

class Course(models.Model):
    """A single course in the system.

    A course consists of shared content, i.e. lectures, tasks etc.
    but is represented to users via an Incarnation.

    A course also holds its own set of files (source code, images, pdfs, etc.)
    with their metadata.

    An Incarnation has a specified subset of the shared course content
    and a subset of users in the whole system.

    A user or a group can be in charge of a course."""

    name = models.CharField(max_length=200)

    def __unicode__(self):
        return self.name

def get_upload_path(instance, filename):
    import os
    return os.path.join("%s" % instance.course.name, filename)

class File(models.Model):
    """Metadata of a file that a user has uploaded."""

    course = models.ForeignKey(Course)
    uploader = models.ForeignKey(User)

    date_uploaded = models.DateTimeField('date uploaded')
    name = models.CharField(max_length=200)
    typeinfo = models.CharField(max_length=200)
    fileinfo = models.FileField(upload_to=get_upload_path) # TODO: Can this cause problems if course.name has /?
    # https://docs.djangoproject.com/en/dev/ref/models/fields/#filefield

    def __unicode__(self):
        return self.name

class ContentPage(models.Model):
    """A single content containing page of a course.

    May be a part of a course incarnation graph."""
    course = models.ForeignKey(Course)

    name = models.CharField(max_length=200)
    content = models.TextField()

    def __unicode__(self):
        return self.name

class LecturePage(ContentPage):
    """A single page from a lecture."""
    pass

class TaskPage(ContentPage):
    """A single task."""
    question = models.TextField()

class RadiobuttonTask(TaskPage):
    pass

class CheckboxTask(TaskPage):
    pass

class TextfieldTask(TaskPage):
    pass

class FileTask(TaskPage):
    pass

class FileTaskTest(models.Model):
    pass

class TextfieldTaskAnswer(models.Model):
    task = models.ForeignKey(TextfieldTask)
    correct = models.BooleanField()
    regexp = models.BooleanField()
    answer = models.TextField()
    hint = models.TextField(blank=True)

class RadiobuttonTaskAnswer(models.Model):
    task = models.ForeignKey(RadiobuttonTask)
    correct = models.BooleanField()
    answer = models.TextField()
    hint = models.TextField(blank=True)

    def __unicode__(self):
        return self.answer

class CheckboxTaskAnswer(models.Model):
    task = models.ForeignKey(CheckboxTask)
    correct = models.BooleanField()
    answer = models.TextField()
    hint = models.TextField(blank=True)

    def __unicode__(self):
        return self.answer
    
class Incarnation(models.Model):
    """An incarnation of a course.

    Saves data on which users are partaking and which course material is
    available on this specific instance of a course.

    A user or a group can be in charge of a course incarnation."""
    
    course = models.ForeignKey(Course)
    contents = models.ManyToManyField(ContentPage, blank=True)

    name = models.CharField(max_length=200)
    frozen = models.BooleanField() # If no changes are possible to this instance of the course
    start_date = models.DateTimeField('course begin date')
    end_date = models.DateTimeField('course end date')

    def __unicode__(self):
        return self.name

class ContentGraphNode(models.Model):
    incarnation = models.ForeignKey(Incarnation)
    parentnode = models.ForeignKey('self', null=True, blank=True)
    content = models.ForeignKey(ContentPage)
    name = models.CharField(max_length=30)

    def __unicode__(self):
        return self.name

class ContentGraph(models.Model):
    """Defines the tree (or the graph) of the course content."""
    incarnation = models.ForeignKey(Incarnation)
    starting_node = models.ForeignKey(ContentGraphNode, blank=True)
    name = models.CharField(max_length=50)

    def __unicode__(self):
        return self.name

class Responsible(models.Model):
    """A user or a group that has been assigned as responsible for a course, course incarnation or some specific material."""
    pass