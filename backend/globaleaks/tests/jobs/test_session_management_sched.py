# -*- coding: utf-8 -*-
from twisted.internet.defer import inlineCallbacks

from globaleaks.tests import helpers
from globaleaks.settings import GLSettings

from globaleaks.handlers import authentication
from globaleaks.jobs import session_management_sched

class TestSessionManagementSched(helpers.TestGL):

    @inlineCallbacks
    def test_session_management_sched(self):

        authentication.GLSession('admin', 'admin', 'enabled') # 1!
        authentication.GLSession('admin', 'admin', 'enabled') # 2!
        authentication.GLSession('admin', 'admin', 'enabled') # 3!

        self.assertEqual(len(GLSettings.sessions), 3)
        authentication.reactor_override.advance(GLSettings.defaults.authentication_lifetime)
        self.assertEqual(len(GLSettings.sessions), 0)

        yield session_management_sched.SessionManagementSchedule().operation()
