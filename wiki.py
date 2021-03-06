#!/usr/bin/env python
#
# Copyright 2010 Myles Grant
# Copyright 2008 Google Inc.
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

"""A simple Google App Engine wiki application.

The main distinguishing feature is that editing is in a WYSIWYG editor
rather than a text editor with special syntax.  This application uses
google.appengine.api.datastore to access the datastore.  This is a
lower-level API on which google.appengine.ext.db depends.
"""

__author__ = 'Bret Taylor, Myles Grant'
__copyright__ = "Copyright 2010 Myles Grant, Copyright 2008 Google Inc"
__license__ = "Apache"
__maintainer__ = "Myles Grant"
__email__ = "myles@mylesgrant.com"

import cgi
import datetime
import os
import re
import sys
import urllib
import urlparse
import difflib
import wsgiref.handlers

from google.appengine.api import datastore
from google.appengine.api import datastore_types
from google.appengine.api import users
from google.appengine.ext import webapp
from google.appengine.api import memcache
from google.appengine.ext.webapp import template

# Set to true if we want to have our webapp print stack traces, etc
_DEBUG = True

_VALID_WIKINAME = re.compile(r'([A-Z0-9])\w+(([A-Z0-9])\w+)+')

class BaseRequestHandler(webapp.RequestHandler):
  """Supplies a common template generation function.

  When you call generate(), we augment the template variables supplied with
  the current user in the 'user' variable and the current webapp request
  in the 'request' variable.
  """
  def generate(self, template_name, template_values={}):
    values = {
      'request': self.request,
      'user': users.GetCurrentUser(),
      'login_url': users.CreateLoginURL(self.request.uri),
      'logout_url': users.CreateLogoutURL(self.request.uri),
      'application_name': 'Wiki',
    }
    values.update(template_values)
    directory = os.path.dirname(__file__)
    path = os.path.join(directory, os.path.join('templates', template_name))
    self.response.out.write(template.render(path, values, debug=_DEBUG))

class WikiPage(BaseRequestHandler):
  """Our one and only request handler.

  We first determine which page we are editing, using "MainPage" if no
  page is specified in the URI. We then determine the mode we are in (view
  or edit), choosing "view" by default.

  POST requests to this handler handle edit operations, writing the new page
  to the datastore.
  """
  def get(self, page_name):
    # Load the main page by default
    if not page_name:
      page_name = 'MainPage'

    # TODO: Handle ReservedPages here. These are pages which have their own code and templates
    # and therefore cannot be created/edited. Examples: RecentChanges and PhotoUpload

    # Is page_name valid?
    if not _VALID_WIKINAME.match(page_name):
      self.error(404)
      self.generate('404.html')
      return

    if page_name.count('_'):
      name_clean = page_name.replace('_', '') # Strip all underscores
      self.redirect(self.request.url.replace(page_name, name_clean), permanent=True)

    page = Page.load(page_name)

    # Default to edit for pages that do not yet exist
    if not page.entity:
      mode = 'edit'
    else:
      modes = ['view', 'edit', 'history', 'diff']
      mode = self.request.get('mode')
      if not mode in modes:
        mode = 'view'

    # User must be logged in to edit
    if mode == 'edit' and not users.GetCurrentUser():
      self.redirect(users.CreateLoginURL(self.request.uri))
      return

    # Generate the appropriate template
    if mode == 'history':
      self.generate(mode + '.html', {
        'page': page,
        'history': page.fetch_history(),
      })
    elif mode == 'diff':
      self.generate(mode + '.html', {
        'page': page,
        'diff': page.diff_history(self.request.get('v1'), self.request.get('v2'))
      })
    else:
      self.generate(mode + '.html', {
        'page': page,
      })

  def post(self, page_name):
    # Is page_name valid?
    if not _VALID_WIKINAME.match(page_name):
      self.redirect('/')

    # User must be logged in to edit
    if not users.GetCurrentUser():
      # The GET version of this URI is just the view/edit mode, which is a
      # reasonable thing to redirect to
      self.redirect(users.CreateLoginURL(self.request.uri))
      return

    # Create or overwrite the page
    page = Page.load(page_name)
    page.content = self.request.get('content')
    page.remote_addr = self.request.remote_addr
    page.comment = self.request.get('comment')
    page.save()
    self.redirect(page.view_url())


class Page(object):
  """Our abstraction for a Wiki page.

  We handle all datastore operations so that new pages are handled
  seamlessly. To create OR edit a page, just create a Page instance and
  call save().
  """
  def __init__(self, name, entity=None):
    self.name = name
    self.entity = entity
    if entity:
      self.content = entity['content']
      if entity.has_key('user'):
        self.user = entity['user']
      else:
        self.user = None
      self.created = entity['created']
      if entity.has_key('modified'):
        self.modified = entity['modified']
      else:
        self.modified = entity['created']
    else:
      # New pages should start out with a simple title to get the user going
      now = datetime.datetime.now()
      self.content = '<h1>' + cgi.escape(name) + '</h1>'
      self.user = None
      self.created = now
      self.modified = now

  def entity(self):
    return self.entity

  def history_url(self):
    return '/' + self.name + '?mode=history'

  def edit_url(self):
    return '/' + self.name + '?mode=edit'

  def view_url(self):
    return '/' + self.name

  def wikified_content(self):
    """Applies our wiki transforms to our content for HTML display.

    We auto-link URLs, link WikiWords, and hide referers on links that
    go outside of the Wiki.
    """
    transforms = [
      AutoLink(),
      WikiWords(),
      HideReferers(),
    ]
    data = memcache.get('content_'+self.name)
    if data is not None:
      return data
    else:
      content = self.content
      for transform in transforms:
        content = transform.run(content)
      # TODO: Enable memcache storing of the transformed data, once we track which pages link to others so we can clear
      # the content caches for pages linking to edited pages. Does that make sense? Good.
      #memcache.set('content_'+self.name, content)
      return content

  def save(self):
    """Creates or edits this page in the datastore."""
    now = datetime.datetime.now()
    if self.entity:
      entity = self.entity
    else:
      entity = datastore.Entity('Page')
      entity['name'] = self.name
      entity['created'] = now

    entity['content'] = datastore_types.Text(self.content)
    entity['modified'] = now

    if users.GetCurrentUser():
      entity['user'] = users.GetCurrentUser()
    elif entity.has_key('user'):
      del entity['user']

    datastore.Put(entity)
    memcache.delete('page_'+self.name)
    memcache.delete('content_'+self.name)

    # Any time the page is saved, we store a copy in history
    self.save_to_history()

  def save_to_history(self):
    """Saves this page in the history datastore."""
    entity = datastore.Entity('PageHistory')
    entity['name'] = self.name
    entity['created'] = self.modified
    entity['content'] = datastore_types.Text(self.content)
    entity['remote_addr'] = self.remote_addr
    entity['comment'] = self.comment

    if users.GetCurrentUser():
      entity['user'] = users.GetCurrentUser()

    datastore.Put(entity)

  def fetch_history(self):
    """Fetch the history of this page."""
    query = datastore.Query('PageHistory')
    query['name ='] = self.name
    query.Order(('created', datastore.Query.DESCENDING))
    return query.Get(1000)

  def diff_history(self, v1, v2):
    """Return the diff of two versions of this page, or None if either of the versions doesn't exist"""
    page_v1 = Page.load_from_history(self.name, v1)
    page_v2 = Page.load_from_history(self.name, v2)

    if not page_v1 or not page_v2:
	return None

    v1_desc = "Edited on %s by %s" % (page_v1.created.strftime("%a, %b %d, %Y at %I:%M %p"), page_v1.user.nickname())
    v2_desc = "Edited on %s by %s" % (page_v2.created.strftime("%a, %b %d, %Y at %I:%M %p"), page_v2.user.nickname())
    diff = difflib.HtmlDiff().make_table(page_v1.content.splitlines(1), page_v2.content.splitlines(1), v1_desc, v2_desc, context=True)
    return diff

  @staticmethod
  def load(name):
    """Loads the page with the given name.

    We always return a Page instance, even if the given name isn't yet in
    the database. In that case, the Page object will be created when save()
    is called.
    """
    data = memcache.get('page_'+name)
    if data is not None:
      return Page(name, data)
    else:
      query = datastore.Query('Page')
      query['name ='] = name.replace('_', '') # Strip all underscores
      entities = query.Get(1)
      if len(entities) < 1:
        return Page(name)
      else:
	memcache.set('page_'+name, entities[0])
        return Page(name, entities[0])

  @staticmethod
  def exists(name):
    """Returns true if the page with the given name exists in the datastore."""
    return Page.load(name).entity

  @staticmethod
  def load_from_history(name, key):
    """Loads the page with the given name and key from history."""
    query = datastore.Query('PageHistory')
    query['name ='] = name
    query['__key__ ='] = datastore_types.Key(key)
    entities = query.Get(1)
    if len(entities) < 1:
      return None
    else:
      return Page(name, entities[0])

class Transform(object):
  """Abstraction for a regular expression transform.

  Transform subclasses have two properties:
     regexp: the regular expression defining what will be replaced
     replace(MatchObject): returns a string replacement for a regexp match

  We iterate over all matches for that regular expression, calling replace()
  on the match to determine what text should replace the matched text.

  The Transform class is more expressive than regular expression replacement
  because the replace() method can execute arbitrary code to, e.g., look
  up a WikiWord to see if the page exists before determining if the WikiWord
  should be a link.
  """
  def run(self, content):
    """Runs this transform over the given content.

    We return a new string that is the result of this transform.
    """
    parts = []
    offset = 0
    for match in self.regexp.finditer(content):
      parts.append(content[offset:match.start(0)])
      parts.append(self.replace(match))
      offset = match.end(0)
    parts.append(content[offset:])
    return ''.join(parts)


class WikiWords(Transform):
  """Translates WikiWords to links.

  We look up all words, and we only link those words that currently exist.
  """
  def __init__(self):
    self.regexp = _VALID_WIKINAME

  def replace(self, match):
    wikiword = match.group(0)
    if Page.exists(wikiword):
      return '<a class="wikiword" href="/%s">%s</a>' % (wikiword, wikiword)
    else:
      return '<a title="%s does not exist yet. Click to create it." class="wikiword_new" href="/%s?mode=edit">%s?</a>' % (wikiword, wikiword, wikiword)


class AutoLink(Transform):
  """A transform that auto-links URLs."""
  def __init__(self):
    self.regexp = re.compile(r'([^"])\b((http|https)://[^ \t\n\r<>\(\)&"]+' \
                             r'[^ \t\n\r<>\(\)&"\.])')

  def replace(self, match):
    url = match.group(2)
    return match.group(1) + '<a class="autourl" href="%s">%s</a>' % (url, url)


class HideReferers(Transform):
  """A transform that hides referers for external hyperlinks."""

  def __init__(self):
    self.regexp = re.compile(r'href="(http[^"]+)"')

  def replace(self, match):
    url = match.group(1)
    scheme, host, path, parameters, query, fragment = urlparse.urlparse(url)
    url = 'http://www.google.com/url?sa=D&amp;q=' + urllib.quote(url)
    return 'href="' + url + '"'


def main():
  application = webapp.WSGIApplication([
    ('/(.*)', WikiPage),
  ], debug=_DEBUG)
  wsgiref.handlers.CGIHandler().run(application)


if __name__ == '__main__':
  main()
