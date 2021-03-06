# -*- encoding: utf-8 -*-
#
#   mailflush_sched
#   ***************
#
# Flush the email that has to be sent, is based on EventLog
# database table.

from cyclone.util import ObjectDict as OD
from storm.expr import Asc
from twisted.internet.defer import inlineCallbacks, returnValue

from globaleaks.orm import transact, transact_ro
from globaleaks.models import EventLogs, Notification
from globaleaks.handlers.admin.node import db_admin_serialize_node
from globaleaks.handlers.admin.notification import admin_serialize_notification
from globaleaks.jobs.base import GLJob
from globaleaks.settings import GLSettings
from globaleaks.notification import MailNotification, update_event_notification_status
from globaleaks.utils.mailutils import sendmail
from globaleaks.utils.utility import deferred_sleep, log
from globaleaks.utils.templating import Templating

reactor_override = None


@transact_ro
def load_complete_events(store, events_limit=GLSettings.notification_limit):
    """
    This function do not serialize, but make an OD() of the description.
    events_limit represent the amount of event that can be returned by the function,
    events to be notified are taken in account later.
    """
    node_desc = db_admin_serialize_node(store, GLSettings.memory_copy.default_language)

    event_list = []

    totaleventinqueue = store.find(EventLogs, EventLogs.mail_sent == False).count()
    storedevnts = store.find(EventLogs, EventLogs.mail_sent == False)[:events_limit * 3]

    debug_event_counter = {}
    for i, stev in enumerate(storedevnts):
        if len(event_list) == events_limit:
            log.debug("Reached maximum number of event notifications doable on a single loop %d" % events_limit)
            break

        debug_event_counter.setdefault(stev.event_reference['kind'], 0)
        debug_event_counter[stev.event_reference['kind']] += 1

        if not stev.description['receiver_info']['tip_notification']:
            continue

        eventcomplete = OD()

        # node level information are not stored in the node, but fetch now
        eventcomplete.notification_settings = admin_serialize_notification(
            store.find(Notification).one(), stev.description['receiver_info']['language']
        )

        eventcomplete.node_info = node_desc

        # event level information are decoded form DB in the old 'Event'|nametuple format:
        eventcomplete.receiver_info = stev.description['receiver_info']
        eventcomplete.tip_info = stev.description['tip_info']
        eventcomplete.subevent_info = stev.description['subevent_info']
        eventcomplete.context_info = stev.description['context_info']

        eventcomplete.type = stev.description['type'] # 'Tip', 'Comment'
        eventcomplete.trigger = stev.event_reference['kind'] # 'blah' ...

        eventcomplete.orm_id = stev.id

        event_list.append(eventcomplete)

    if debug_event_counter:
        if totaleventinqueue > (events_limit * 3):
            log.debug("load_complete_events: %s from %d Events" %
                      (debug_event_counter, totaleventinqueue))
        else:
            log.debug("load_complete_events: %s from %d Events, with a protection limit of %d" %
                      (debug_event_counter, totaleventinqueue, events_limit * 3 ))

    return event_list


def filter_notification_event(notifque):
    """
    :param notifque: the current notification event queue
    :return: a modified queue in the case some email has not to be sent
    Basically performs two filtering; they are defined in:
     1) issue #444
     2) issue #798
    """

    # Here we collect the Storm event of Files having as key the Tip
    files_event_by_tip = {}

    _tmp_list = []
    return_filtered_list = []
    # to be smoked Storm.id
    orm_id_to_be_skipped = []

    for ne in notifque:
        if ne['trigger'] !=  u'Tip':
            continue
        files_event_by_tip.update({ne['tip_info']['id'] : []})

    log.debug("Filtering function: iterating over %d Tip" % len(files_event_by_tip.keys()))
    # not files_event_by_tip contains N keys with an empty list,
    # I'm looping two times because dict has random ordering
    for ne in notifque:

        if GLSettings.memory_copy.disable_receiver_notification_emails:
            orm_id_to_be_skipped.append(ne['orm_id'])
            continue

        if ne['trigger'] != u'File':
            _tmp_list.append(ne)
            continue

        if ne['tip_info']['id'] in files_event_by_tip:
            orm_id_to_be_skipped.append(ne['orm_id'])
        else:
            _tmp_list.append(ne)


    if len(orm_id_to_be_skipped):
        if GLSettings.memory_copy.disable_receiver_notification_emails:
            log.debug("All the %d mails will be marked as sent because the admin has disabled receivers notifications" %
                      len(orm_id_to_be_skipped))
        else:
            log.debug("Filtering function: Marked %d Files notification to be suppressed cause part of a submission" %
                      len(orm_id_to_be_skipped))

    for ne in _tmp_list:
        receiver_id = ne['receiver_info']['id']

        sent_emails = GLSettings.get_mail_counter(receiver_id)

        if sent_emails >= GLSettings.memory_copy.notification_threshold_per_hour:
            log.debug("Discarding email for receiver %s due to threshold already exceeded for the current hour" %
                      receiver_id)
            orm_id_to_be_skipped.append(ne['orm_id'])
            continue

        GLSettings.increment_mail_counter(receiver_id)

        if sent_emails + 1 >= GLSettings.memory_copy.notification_threshold_per_hour:
            log.info("Reached threshold of %d emails with limit of %d for receiver %s" % (
                sent_emails,
                GLSettings.memory_copy.notification_threshold_per_hour,
                receiver_id)
            )

            # Append
            anomalyevent = OD()
            anomalyevent.type = u'receiver_notification_limit_reached'
            anomalyevent.notification_settings = ne.notification_settings
            anomalyevent.node_info = ne.node_info
            anomalyevent.context_info = None
            anomalyevent.receiver_info = ne.receiver_info
            anomalyevent.tip_info = None
            anomalyevent.subevent_info = None
            anomalyevent.orm_id = 0

            return_filtered_list.append(anomalyevent)

            orm_id_to_be_skipped.append(ne['orm_id'])
            continue

        return_filtered_list.append(ne)

    log.debug("Mails filtering completed passing from #%d to #%d events" %
              (len(notifque), len(return_filtered_list)))

    # return the new list of event and the list of Storm.id
    return return_filtered_list, orm_id_to_be_skipped


class MailflushSchedule(GLJob):
    name = "Mailflush"

    # sorry for the double negation, we are sleeping two seconds below.
    skip_sleep = False

    mail_notification = MailNotification()

    def ping_mail_flush(self, notification_settings, receivers_synthesis):
        for _, data in receivers_synthesis.iteritems():
            receiver_dict, winks = data

            receiver_name = receiver_dict['name']
            receiver_email = receiver_dict['ping_mail_address']

            fakeevent = OD()
            fakeevent.type = u'ping_mail'
            fakeevent.node_info = None
            fakeevent.context_info = None
            fakeevent.receiver_info = receiver_dict
            fakeevent.tip_info = None
            fakeevent.subevent_info = {'counter': winks}

            subject = Templating().format_template(notification_settings['ping_mail_template'], fakeevent)
            body = Templating().format_template(notification_settings['ping_mail_title'], fakeevent)

            return sendmail(receiver_email, subject, body)

    @inlineCallbacks
    def operation(self):
        queue_events = yield load_complete_events()

        if not len(queue_events):
            returnValue(None)

        # remove from this list the event that has not to be sent, for example,
        # the Files uploaded during the first submission, like issue #444 (zombie edition)
        filtered_events, to_be_suppressed = filter_notification_event(queue_events)

        if len(to_be_suppressed):
            for eid in to_be_suppressed:
                yield update_event_notification_status(eid, True)

        for qe in filtered_events:
            yield self.mail_notification.do_every_notification(qe)

            if not self.skip_sleep:
                yield deferred_sleep(2)

        # This is the notification of the ping, if configured
        receivers_synthesis = {}
        for qe in filtered_events:
            if not qe.receiver_info['ping_notification']:
                continue

            if qe.receiver_info['id'] not in receivers_synthesis:
                receivers_synthesis[qe.receiver_info['id']] = [qe.receiver_info, 1]
            else:
                receivers_synthesis[qe.receiver_info['id']][1] += 1

        if len(receivers_synthesis.keys()):
            # I'm taking the element [0] of the list but every element has the same
            # notification settings; this value is passed to ping_mail_flush at
            # it is needed by Templating()
            yield self.ping_mail_flush(filtered_events[0].notification_settings,
                                       receivers_synthesis)
