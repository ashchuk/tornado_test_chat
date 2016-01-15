#!/usr/bin/env python
# -*- coding:utf-8 -*-

import tornado.httpserver
import tornado.ioloop
import tornado.web
import tornado.options
import os.path
import logging
import tornado.escape
import uuid
import functools
import pymongo

from pymongo import MongoClient
from datetime import datetime
from tornado.concurrent import Future
from tornado import gen
from tornado.options import define, options, parse_command_line

define("port", default=8888, help="run on the given port", type=int)
define("debug", default=False, help="run in debug mode")


class MessageBuffer(object):
    def __init__(self, db):
        self.waiters = set()
        self.db = db
        self.cache = self.fill_cache()
        self.cache_size = 10

    def fill_cache(self):
        temp = []
        last_messages = self.db.history.find().sort([('date', pymongo.DESCENDING), ('time', pymongo.DESCENDING)]).limit(
            10)
        for messages in last_messages:
            temp.extend([messages])
        return temp

    def wait_for_messages(self, cursor=None):
        # Construct a Future to return to our caller.  This allows
        # wait_for_messages to be yielded from a coroutine even though
        # it is not a coroutine itself.  We will set the result of the
        # Future when results are available.
        result_future = Future()
        if cursor:
            new_count = 0
            for msg in reversed(self.cache):
                if msg["id"] == cursor:
                    break
                new_count += 1
            if new_count:
                result_future.set_result(self.cache[-new_count:])
                return result_future
        self.waiters.add(result_future)
        return result_future

    def cancel_wait(self, future):
        self.waiters.remove(future)
        # Set an empty result to unblock any coroutines waiting.
        future.set_result([])

    def new_messages(self, messages):
        logging.info("Sending new message to %r listeners", len(self.waiters))
        for future in self.waiters:
            future.set_result(messages)
        self.waiters = set()
        self.cache.extend(messages)
        if len(self.cache) > self.cache_size:
            self.cache = self.cache[-self.cache_size:]

    def write_to_database(self, message):
        self.db.history.insert_one(message)

    def get_history(self):
        return self.db.history.find().sort([('date', pymongo.DESCENDING), ('time', pymongo.DESCENDING)])


# Making this a non-singleton is left as an exercise for the reader.
# global_message_buffer = MessageBuffer()

class MessageNewHandler(tornado.web.RequestHandler):
    def post(self):
        message = {
            "id": str(uuid.uuid4()),
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime('%H:%M:%S'),
            "body": self.get_argument("body"),
            "user": self.get_secure_cookie("user")
        }
        # to_basestring is necessary for Python 3's json encoder,
        # which doesn't accept byte strings.
        message["html"] = tornado.escape.to_basestring(
            self.render_string("message.html", message=message))
        if self.get_argument("next", None):
            self.redirect(self.get_argument("next"))
        else:
            self.write(message)
        self.application.message_buffer.new_messages([message])
        self.application.message_buffer.write_to_database(message)


class MessageUpdatesHandler(tornado.web.RequestHandler):
    @gen.coroutine
    def post(self):
        cursor = self.get_argument("cursor", None)
        # Save the future returned by wait_for_messages so we can cancel
        # it in wait_for_messages
        self.future = self.application.message_buffer.wait_for_messages(cursor=cursor)
        messages = yield self.future
        if self.request.connection.stream.closed():
            return
        self.write(dict(messages=messages))

    def on_connection_close(self):
        self.application.message_buffer.cancel_wait(self.future)


def cacheProtectorDecorator(method):
    @tornado.web.authenticated
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self.set_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.set_header('Pragma', 'no-cache')
        self.set_header('Expires', '0')
        return method(self, *args, **kwargs)

    return wrapper


class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        return self.get_secure_cookie("user")


class MainHandler(BaseHandler):
    @cacheProtectorDecorator
    def get(self):
        self.render('chat.html', messages=self.application.message_buffer.cache, user=self.current_user)


class LoginHandler(BaseHandler):
    def get(self):
        if self.get_secure_cookie("user"):
            logged = 'Hello again!</br> You are already logged as ' + \
                     str(self.get_secure_cookie("user")) + \
                     '.</br><a href="/logout">Logout</a>'
            self.write(logged)
        else:
            self.render('login.html')

    def post(self):
        checked = False
        db = self.application.db
        getusername = self.get_argument("username")
        getpassword = self.get_argument("password")
        data = {'user': getusername, 'password': getpassword}
        # TODO : Check data from DB
        cursor = db.users.find()
        for items in cursor:
            item = {'user': items["user"], 'password': items["password"]}
            if data == item:
                checked = True
                logging.info("Checked! User: %s, Password: %s" % (data["user"], data["password"]))
                break
        if checked:
            logging.info("User was found. User: %s, Password: %s" % (data["user"], data["password"]))
            self.set_secure_cookie("user", self.get_argument("username"))
            self.redirect(self.reverse_url("chat"))
        else:
            logging.info("User not found. User: %s, Password: %s" % (data["user"], data["password"]))
            self.write(
                'You are not registred!</br><a href="/login">Back</a> </br><a href="/registration">Registration</a>')


class RegistrationHandler(BaseHandler):
    def get(self):
        self.render('registration.html')

    def post(self):
        checked = False
        db = self.application.db
        getusername = self.get_argument("username")
        getpassword = self.get_argument("password")
        data = {'user': getusername, 'password': getpassword}
        # TODO : Check data from DB
        cursor = db.users.find()
        for items in cursor:
            item = {'user': items["user"], 'password': items["password"]}
            if data == item:
                checked = True
                logging.info("Checked! User: %s, Password: %s" % (data["user"], data["password"]))
                break
        if checked:
            self.write('You already registred.</br>Forgot pass? Ask administrator</br><a href="/login">Back</a> </br>')
        else:
            db.users.insert_one(data)
            logging.info("New user registred in system! User: %s, Password: %s" % (data["user"], data["password"]))
            self.write('Registred! Now you can login</br><a href="/login">Back</a> </br>')


class LogoutHandler(BaseHandler):
    @cacheProtectorDecorator
    def get(self):
        self.clear_cookie("user")
        self.redirect(self.get_argument("next", self.reverse_url("login")))


class NotFoundRequestHandler(BaseHandler):
    def get(self):
        self.set_status(404)
        self.write('Not found, sorry. </br><a href="/login">Login page</a> </br>')


class MessageHistoryHandler(BaseHandler):
    def get(self):
        self.render('history.html', history=self.application.message_buffer.get_history())


class Application(tornado.web.Application):
    def __init__(self):
        base_dir = os.path.dirname(__file__)
        settings = {
            "cookie_secret": "bZJc2sWbQLKos6GkHn/VB9oXwQt8S0R0kRvJ5/xJ89E=",
            "login_url": "/login",
            'template_path': os.path.join(base_dir, "templates"),
            'static_path': os.path.join(base_dir, "static"),
            'debug': True,
            "xsrf_cookies": True,
        }

        tornado.web.Application.__init__(self, [
            tornado.web.url(r"/chat", MainHandler, name="chat"),
            tornado.web.url(r"/login", LoginHandler, name="login"),
            tornado.web.url(r"/logout", LogoutHandler, name="logout"),
            tornado.web.url(r"/registration", RegistrationHandler, name="registration"),
            tornado.web.url(r"/a/message/new", MessageNewHandler),
            tornado.web.url(r"/a/message/updates", MessageUpdatesHandler),
            tornado.web.url(r"/history", MessageHistoryHandler),
            tornado.web.url(r"/.*", NotFoundRequestHandler)
        ], **settings)
        self.client = MongoClient('localhost', 27017)
        self.db = self.client['chat']
        self.message_buffer = MessageBuffer(self.db)


def main():
    tornado.options.parse_command_line()
    Application().listen(options.port)
    tornado.ioloop.IOLoop.instance().start()


if __name__ == "__main__":
    main()
