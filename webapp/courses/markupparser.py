"""
Parser for wiki markup block content, i.e. paragraphs, bullet lists, tables, etc.
Idea from http://wiki.sheep.art.pl/Wiki%20Markup%20Parser%20in%20Python
"""

import re
import itertools
#from django.utils.html import escape # Escapes ' characters -> prevents inline parsing
from cgi import escape # Use this instead? Security? HTML injection?

from slugify import slugify

import pygments
from pygments.lexers import get_lexer_by_name
from pygments.formatters import HtmlFormatter

import courses.blockparser as blockparser

# TODO: Support indented blocks (e.g. <pre>) within indents, uls & ols
# TODO: Support admonitions/warnings/good to know boxes/etc.
# TODO: Support tags that, when hovered, highlight lines in source code files
# TODO: Support tags that get highlighted upon receiving hints
# TODO: Support tags for monospace ASCII art with horizontal and vertical rulers
# TODO: Support embeddable JavaScript apps (maybe in iframe?)
#       - http://www.html5rocks.com/en/tutorials/security/sandboxed-iframes/

class ParserUninitializedError(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)

class InvalidParserError(Exception):
    # TODO: Add the ability to trace to the defunctional regex
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)

class MarkupParser:
    """
    Static parser class for generating HTML from the used markup block types.

    Each markup component (given to this parser class as a markup class)
    provides its own markup as a regexp and block & settings functions. The
    markups are combined to form the markup language.
    """

    # TODO: Handle the inline markups and HTML escapes on the same pass

    _markups = {}
    _block_re = None
    _inline_re = None
    _ready = False
    
    @classmethod
    def add(cls, *markups):
        """
        Add the Markup classes given as arguments into the parser's internal
        dictionary and set the ready flag False to indicate that re-compilation
        is required.
        """
        cls._ready = False
        cls._markups.update((markup.shortname, markup) for markup in markups)

    @classmethod
    def compile(cls):
        """
        Iterate the parser's internal markup dictionary to create and compile
        the parsing regex based on individual regexes of the different Markup
        classes.
        """
        try:
            cls._block_re = re.compile(
                r"|".join(
                    r"(?P<%s>%s)" % (shortname, markup.regexp)
                    for shortname, markup in sorted(cls._markups.items())
                    if markup.regexp and not markup.inline
                )
            )
        except re.error as e:
            raise InvalidParserError("invalid regex syntax in a markup: %s" % e)

        cls._ready = True

    @classmethod
    def _get_line_kind(cls, line):
        """
        Key function for itertools.groupby(...)

        When a line matches the compiled regexp, select the name of the matched
        group (the shortname attribute of the specifically matching Markup) as
        the key. Otherwise, default the key to 'paragraph'.

        The match object is returned for use in the settings function of the
        markup.        
        """
        matchobj = cls._block_re.match(line)
        return getattr(matchobj, "lastgroup", "paragraph"), matchobj

    @classmethod
    def parse(cls, text):
        """
        A generator that gets the text written in the markup language, splits
        it at newlines and yields the parsed text until the whole text has
        been parsed.
        """
        if not cls._ready:
            raise ParserUninitializedError("compile() not called")

        # TODO: Generator version of splitter to avoid memory & CPU overhead of
        # first creating a complete list and afterwards iterating through it.
        # I.e. reduce from O(2n) to O(n)
        lines = iter(re.split(r"\r\n|\r|\n", text))

        # Note: stateless single-pass parsing of HTML-like languages is
        # impossible because of the closing tags.
        # TODO: Initialize states from markups
        state = {"lines": lines, "list": []}

        for (block_type, matchobj), block in itertools.groupby(lines, cls._get_line_kind):
            block_func = cls._markups[block_type].block
            settings = cls._markups[block_type].settings(matchobj)
            
            # TODO: Modular cleanup of indent, ul, ol, table etc.
            if block_type != "list":
                for undent_lvl in reversed(state["list"]):
                    yield '</%s>' % undent_lvl
                state["list"] = []

            yield from block_func(block, settings, state)

        # Clean up the remaining open tags
        for undent_lvl in reversed(state["list"]):
            yield '</%s>' % undent_lvl

markups = []

# inline = this markup is inline
# allow_inline = if use of inline markup, such as <b> is allowed
class Markup:
    name = ""
    shortname = ""
    description = ""
    regexp = r""
    markup_class = ""
    example = ""
    states = {}
    inline = False
    allow_inline = False

    @classmethod
    def block(cls, block, settings, state):
        pass

    @classmethod
    def settings(cls, matchobj):
        pass

class CalendarMarkup(Markup):
    name = "Calendar"
    shortname = "calendar"
    description = "A calendar for time reservations."
    regexp = r"^\<\!calendar\=(?P<calendar_name>[^\s>]+)\>\s*$"
    markup_class = "embedded item"
    example = "<!calendar=course-project-demo-calendar>"
    inline = False
    allow_inline = False

    @classmethod
    def block(cls, block, settings, state):
        # TODO: embedded_calendar custom template tag
        # TODO: On the other hand, no (security risk).
        yield '{%% embedded_calendar "%s" %%}' % settings["calendar_name"]

    @classmethod
    def settings(cls, matchobj):
        settings = {"calendar_name" : matchobj.group("calendar_name")}
        return settings

markups.append(CalendarMarkup)

class CodeMarkup(Markup):
    name = "Code"
    shortname = "code"
    description = "Monospaced field for code and other preformatted text."
    regexp = r"^[{]{3}(highlight=(?P<highlight>[^\s]*))?\s*$" # TODO: Better settings
    markup_class = ""
    example = ""
    states = {}
    inline = False
    allow_inline = False

    @classmethod
    def block(cls, block, settings, state):
        # TODO: Code syntax highlighting
        highlight = settings["highlight"]
        yield '<pre class="normal">' # TODO: Change class!
        text = ""
        if highlight:
            try:
                lexer = get_lexer_by_name(highlight)
            except pygments.util.ClassNotFound as e:
                yield '<div class="warning">%s</div>' % str(e).capitalize()
                highlight = False
            else:
                yield '<code class="highlight %s">' % highlight

        try:
            line = next(state["lines"])
            while not line.startswith("}}}"):
                text += line + "\n"
                line = next(state["lines"])
        except StopIteration:
            yield 'Warning: unclosed code block!\n'
        if highlight:
            highlighted = pygments.highlight(text[:-1], lexer, HtmlFormatter(nowrap=True))
            yield '%s</code>' % highlighted
        else:
            yield text
        yield '</pre>\n'

    @classmethod
    def settings(cls, matchobj):
        settings = {"highlight" : matchobj.group("highlight")}
        return settings

markups.append(CodeMarkup)

class EmbeddedPageMarkup(Markup):
    name = "Embedded page"
    shortname = "embedded_page"
    description = "A lecture or exercise, embedded into the page in question."
    regexp = r"^\<\!page\=(?P<page_slug>[^\s>]+)\>\s*$"
    markup_class = "embedded item"
    example = "<!page=slug-of-some-exercise>"
    inline = False
    allow_inline = False

    @classmethod
    def block(cls, block, settings, state):
        # TODO: embedded_page custom template tag (inclusion tag?)
        # TODO: On the other hand, no (security risk).
        yield '<div class="embedded-page">\n'
        yield '{%% embedded_page "%s" %%}\n' % settings["page_slug"]
        yield '</div>\n'
        try:
            state["embedded_pages"].append(settings["page_slug"])
        except KeyError:
            state["embedded_pages"] = [settings["page_slug"]]

    @classmethod
    def settings(cls, matchobj):
        settings = {"page_slug" : matchobj.group("page_slug")}
        return settings

markups.append(EmbeddedPageMarkup)

class EmptyMarkup(Markup):
    name = "Empty"
    shortname = "empty"
    description = ""
    regexp = "^\s*$"
    markup_class = ""
    example = ""
    inline = False
    allow_inline = False

    @classmethod
    def block(cls, block, settings, state):
        yield ''

    @classmethod
    def settings(cls, matchobj):
        pass

markups.append(EmptyMarkup)

class HeadingMarkup(Markup):
    name = "Heading"
    shortname = "heading"
    description = ""
    regexp = r"^\s*(?P<level>\={1,6})\=*\s*.+\s*(?P=level)\s*$"
    markup_class = ""
    example = ""
    inline = False
    allow_inline = False

    @classmethod
    def block(cls, block, settings, state):
        heading = ''
        for line in block:
            heading += escape(line.strip("= \r\n\t"))
        slug = slugify(heading)
        yield '<h%d id="%s">' % (settings["heading_level"], slug)
        yield heading
        yield '<a href="#%s" class="permalink" title="Permalink to this heading">&para;</a>' % slug
        yield '</h%d>\n' % settings["heading_level"]
    
    @classmethod
    def settings(cls, matchobj):
        settings = {"heading_level" : len(matchobj.group("level"))}
        return settings

markups.append(HeadingMarkup)

class ImageMarkup(Markup):
    name = "Image"
    shortname = "image"
    description = "An image, img tag in HTML."
    regexp = r"^\<\!image\=(?P<image_name>[^>|]+)(\|alt\=(?P<alt_text>[^>|]+))?\>\s*$"
    markup_class = "embedded item"
    example = "<!image=name-of-some-image.png|alt=alternative text>"
    inline = False
    allow_inline = False

    @classmethod
    def block(cls, block, settings, state):
        # TODO: Implement embedded_image template tag
        # TODO: On the other hand, no (security risk).
        if "alt_text" in settings.keys():
            yield '<img src="{%% embedded_image \'%s\' %%}" alt="%s">\n' % (settings["image_name"], settings["alt_text"])
        else:
            yield '<img src="{%% embedded_image \'%s\' %%}">\n' % settings["image_name"]

    @classmethod
    def settings(cls, matchobj):
        settings = {"image_name" : escape(matchobj.group("image_name"))}
        try:
            settings["alt_text"] = escape(matchobj.group("alt_text"))
        except AttributeError:
            pass
        return settings

markups.append(ImageMarkup)

class ListMarkup(Markup):
    name = "List"
    shortname = "list"
    description = "Unordered and ordered lists."
    regexp = r"^(?P<list_level>[*#]+)(?P<text>.+)$"
    markup_class = ""
    example = "* unordered list item 1\n** indented unordered list item 1\n"\
              "# ordered list item 1\n## indented ordered list item 1\n"
    states = {"list" : []}
    inline = False
    allow_inline = True

    @classmethod
    def block(cls, block, settings, state):
        tag = settings["tag"]

        if len(state["list"]) < settings["level"]:
            for new_lvl in range(settings["level"] - len(state["list"])):
                state["list"].append(tag)
                yield '<%s>' % tag
        elif len(state["list"]) > settings["level"]:
            for new_lvl in range(len(state["list"]) - settings["level"]):
                top_tag = state["list"].pop()
                yield '</%s>' % top_tag
        
        if len(state["list"]) == settings["level"]:
            if state["list"][-1] != tag:
                top_tag = self.list_state.pop()
                yield '</%s>' % top_tag
                
                state["list"].append(tag)
                yield '<%s>' % tag
        
        for line in block:
            yield '<li>%s</li>' % blockparser.parseblock(escape(line.strip("*#").strip()))

    @classmethod
    def settings(cls, matchobj):
        list_level = matchobj.group("list_level")
        settings = {"level" : len(list_level),
                    #"text" : matchobj.group("text").strip(),
                    "tag" : "ul" if list_level[-1] == "*" else "ol"}
        return settings

markups.append(ListMarkup)

class ParagraphMarkup(Markup):
    name = "Paragraph"
    shortname = "paragraph"
    description = "A paragraph of text, p tag in HTML."
    regexp = r""
    markup_class = "text"
    example = "Text without any of the block level markups."
    inline = False
    allow_inline = True

    @classmethod
    def block(cls, block, settings, state):
        yield '<p>'
        paragraph = ""
        paragraph_lines = []
        for line in block:
            paragraph_lines.append(escape(line))
        paragraph = "<br>\n".join(paragraph_lines)
        paragraph = blockparser.parseblock(paragraph)
        yield paragraph
        yield '</p>\n'

    @classmethod
    def settings(cls, matchobj):
        pass

markups.append(ParagraphMarkup)

class SeparatorMarkup(Markup):
    name = "Separator"
    shortname = "separator"
    description = "A separating horizontal line, hr tag in HTML."
    regexp = r"^\s*\-{2}\s*$"
    markup_class = "miscellaneous"
    example = "--"
    inline = False
    allow_inline = False
    
    @classmethod
    def block(cls, block, settings, state):
        yield '<hr>\n'

    @classmethod
    def settings(cls, matchobj):
        pass

markups.append(SeparatorMarkup)

class SourceCodeMarkup(Markup):
    name = "Source code file"
    shortname = "sourcecodefile"
    description = "A listing of uploaded source code."
    regexp = r"^\<\!sourcecodefile\=(?P<source_filename>[^>]+)\>\s*$"
    markup_class = "embedded item"
    example = "<!sourcecodefile=hello_world.py>"
    inline = False
    allow_inline = False

    @classmethod
    def block(cls, block, settings, state):
        # TODO: embedded_sourcecode custom template tag
        # TODO: On the other hand, no (security risk).
        yield '{%% embedded_sourcecode "%s" %%}' % settings["source_filename"]

    @classmethod
    def settings(cls, matchobj):
        settings = {"source_filename": matchobj.group("source_filename")}
        return settings

markups.append(SourceCodeMarkup)

class TableMarkup(Markup):
    name = "Table"
    shortname = "table"
    description = "A table for representing tabular information."
    regexp = r"^[|]{2}([^|]+[|]{2})+\s*$"
    markup_class = ""
    example = "|| Heading 1 || Heading 2 ||"
    states = {}
    inline = False
    allow_inline = False

    @classmethod
    def block(cls, block, settings, state):
        yield '<table>'
        for line in block:
            row = line.split("||")[1:-1]
            yield '<tr>'
            yield '\n'.join("<td>%s</td>" % blockparser.parseblock(escape(cell)) for cell in row)
            yield '</tr>'
        yield '</table>'

    @classmethod
    def settings(cls, matchobj):
        pass

markups.append(TableMarkup)

class TeXMarkup(Markup):
    name = "TeX"
    shortname = "tex"
    description = ""
    regexp = r"^[<]math[>]\s*$"
    markup_class = ""
    example = ""
    inline = False
    allow_inline = False

    @classmethod
    def block(cls, block, settings, state):
        yield '<div class="tex">'
        try:
            line = next(state["lines"])
            while not line.startswith("</math>"):
                yield escape(line) + "\n"
                line = next(state["lines"])
        except StopIteration:
            # TODO: Modular, class-based warning system
            yield 'Warning: unclosed TeX block!\n'
        yield '</div>\n'

    @classmethod
    def settings(cls, matchobj):
        pass

markups.append(TeXMarkup)

# TODO: Add indentation support to all compatible markups.

MarkupParser.add(*markups)
MarkupParser.compile()
