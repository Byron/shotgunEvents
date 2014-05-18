#-*-coding:utf-8-*-
"""
@package sgevents.engine
@brief Contains the core of the evnet processing framework

@author Sebastian Thiel
@copyright [MIT License](http://www.opensource.org/licenses/mit-license.php)
"""
__all__ = []


import time
import logging
import socket
import cPickle as pickle

import shotgun_api3 as sg

from bshotgun import ProxyShotgunConnection
from bapp import ApplicationSettingsMixin
from butility import TerminatableThread

from .plugin import EventEnginePluginCollection
from .utility import (CustomSMTPHandler,
                      engine_schema,
                      set_file_path_on_logger,
                      EventEngineError)


class EventEngine(TerminatableThread, ApplicationSettingsMixin):
    """
    The engine holds the main loop of event processing.
    @note the caller is supposed to handle signals and let us know about it (just in case we are 
        not aborted anyway as we aer in a thread)
    """

    __slots__ = ('log',
                 '_event_id_data', 
                 '_sg')

    _schema = engine_schema

    ## Name used in logs as prefix
    LOG_NAME = 'sg-events-engine'

    # -------------------------
    ## @name Configuration
    # @{

    ## A type which can create a shotgun connection without any arguments
    ProxyShotgunConnectionType = ProxyShotgunConnection
    
    ## -- End Configuration -- @}

    def __init__(self, sg_connection = None):
        """
        Initialize this instance, with an optional sg_connection.
        @param sg_connection if unset, it will be created from ProxyShotgunConnectionType. Useful for testing,
        as the entire connection can be mocked as needed
        """
        self._event_id_data = {}

        # Get config values
        self._plugin_collections = [EventEnginePluginCollection(self, s) for s in self.config.getPluginPaths()]

        self._sg = sg_connection or self.ProxyShotgunConnectionType()

        config = self.settings_value()

        # Setup the logger for the main engine
        self.log = logging.getLogger(self.LOG_NAME)
        if config.logging['one-file-per-plugin'] == 0:
            # Set the root logger for file output.
            rootLogger = logging.getLogger()
            set_file_path_on_logger(rootLogger, config.logging.path.expandvars())

            # Set the engine logger for email output.
            self.set_emails_on_logger(self.log, True)
        else:
            # Set the engine logger for file and email output.
            set_file_path_on_logger(self.log, config.logging.path.expandvars())
            self.set_emails_on_logger(self.log, True)
        # end handle initial logging configuration



    # -------------------------
    ## @name Plugin Interface
    # For use by EventEnginPlugins
    # @{
    
    @classmethod
    def set_emails_on_logger(cls, logger, emails):
        """Configures a logger to either receive emails or not. In any case, existing handlers will be removed
        @param logger 
        @param emails either True, False, or a list of e-mail to addresses to send emails to.
        If False, existing smtp handlers will be removed. If True, e-email to addresses will be reset to the 
        ones configured for the engine.
        Otherwise it is expected to be a list or tuple of e-mail to-addresses."""
        # Configure the logger for email output
        remove_handlers_from_logger(logger, logging.handlers.SMTPHandler)

        if emails is False:
            return

        config = cls.settings_value().logging.email

        username = config.username or None
        password = config.password or None

        if emails is True:
            to_addrs = config.to
        elif isinstance(emails, (list, tuple)):
            to_addrs = emails
        else:
            msg = 'Argument emails should be True to use the default addresses, False to not send any emails or a list of recipient addresses. Got %s.'
            raise ValueError(msg % type(emails))
        # end handle emails arg

        CustomSMTPHandler.add_to_logger(logger, config['host'], config.from_addr, to_addrs, config.subject,
                                        username, password)

    ## -- End Plugin Interface -- @}

    def run(self):
        """
        Start the processing of events.

        The last processed id is loaded up from persistent storage on disk and
        the main loop is started.

        @note usually called as part of threading, but will work without it as well
        """
        config = self.settings_value()
        socket.setdefaulttimeout(config['socket-timeout'].seconds)

        # Notify which version of shotgun api we are using
        self.log.info('Using Shotgun version %s' % sg.__version__)

        try:
            for collection in self._plugin_collections:
                collection.load()

            self._load_event_id_data()

            self._main_loop()
        except Exception as err:
            self.log.critical('Unexpected error (%s) in main loop.', type(err), exc_info=True)
        # end exception handling

    def _load_event_id_data(self):
        """
        Load the last processed event id from the disk

        If no event has ever been processed or if the event_id_file has been
        deleted from disk, no id will be recoverable. In this case, we will try
        contacting Shotgun to get the latest event's id and we'll start
        processing from there.
        @throws EventEngineError
        """
        config = self.settings_value()
        event_id_file = config['event-journal-file'].expandvars()

        read_journal = False
        if event_id_file.exists():
            try:
                fh = open(event_id_file, 'rb')
                try:
                    self._event_id_data = pickle.load(fh)

                    # Provide event id info to the plugin collections. Once
                    # they've figured out what to do with it, ask them for their
                    # last processed id.
                    for collection in self._plugin_collections:
                        state = self._event_id_data.get(collection.path)
                        if state:
                            collection.set_state(state)
                        # end have state for collection
                    # end for each collection
                    read_journal = True
                except pickle.UnpicklingError as err:
                    self.log.error(str(err))
                # end ignore 
                fh.close()
            except OSError as err:
                # this must stop operation !
                raise EventDaemonError("Could not open event journal at '%s'.", event_id_file, exc_info=True)
            # end convert OSErrors
        # end try reading event journal

        if not read_journal:
            # No id file?
            # Get the latest event data from the database.
            conn_attempts = 0
            last_event_id = None
            while last_event_id is None:
                order = [{'column':'id', 'direction':'desc'}]
                try:
                    result = self._sg.find_one("EventLogEntry", filters=[], fields=['id'], order=order)
                except (sg.ProtocolError, sg.ResponseError, socket.err) as err:
                    conn_attempts = self._check_connection_attempts(conn_attempts, str(err))
                except Exception as err:
                    msg = "Unknown error: %s" % str(err)
                    conn_attempts = self._check_connection_attempts(conn_attempts, msg)
                else:
                    last_event_id = result['id']
                    self.log.info('Last event id (%d) from the Shotgun database.', last_event_id)

                    # pretend for this was the last id for plugins as well, even though they 
                    # didn't actually process it
                    for collection in self._plugin_collections:
                        collection.set_state(last_event_id)
                    # end 
                # end 
            # end

            self._save_event_id_data()
        # end no journal exists

    def _main_loop(self):
        """
        Run the event processing loop.

        General behavior:
        - Load plugins from disk - see L{load} method.
        - Get new events from Shotgun
        - Loop through events
        - Loop through each plugin
        - Loop through each callback
        - Send the callback an event
        - Once all callbacks are done in all plugins, save the eventId
        - Go to the next event
        - Once all events are processed, wait for the defined fetch interval time and start over.

        Caveats:
        - If a plugin is deemed "inactive" (an error occured during
          registration), skip it.
        - If a callback is deemed "inactive" (an error occured during callback
          execution), skip it.
        """
        self.log.debug('Starting the event processing loop.')
        while not self._should_terminate():
            # Process events
            for event in self._fetch_new_events():
                for collection in self._plugin_collections:
                    collection.process(event)
                self._save_event_id_data()
            # end for each event to dispatch

            config = self.settings_value()
            time.sleep(config['poll-every'].seconds)

            # Reload plugins
            for collection in self._plugin_collections:
                collection.load()
            # end for each load path to reload
                
            # Make sure that newly loaded events have proper state.
            self._load_event_id_data()
        # end while we shouldn't terminate

        self.log.debug('Shuting down event processing loop.')

    def _fetch_new_events(self):
        """
        Fetch new events from Shotgun.

        @return: Recent events that need to be processed by the engine.
        @rtype: I{list} of Shotgun event dictionaries.
        """
        next_event_id = None
        for new_id in [coll.next_unprocessed_event_id() for coll in self._plugin_collections]:
            if new_id is not None and (next_event_id is None or new_id < next_event_id):
                next_event_id = new_id
        # end find smallest event id

        if next_event_id is None:
            return list()
        # end bail out early

        filters = [['id', 'greater_than', next_event_id - 1]]
        fields = ['id', 'event_type', 'attribute_name', 'meta', 'entity', 'user', 'project', 'session_uuid']
        order = [{'column':'id', 'direction':'asc'}]

        conn_attempts = 0
        while True:
            try:
                return self._sg.find("EventLogEntry", filters=filters, fields=fields, 
                                      order=order, filter_operator='all')
            except (sg.ProtocolError, sg.ResponseError, socket.error) as err:
                conn_attempts = self._check_connection_attempts(conn_attempts, str(err))
            except Exception as err:
                msg = "Unknown error: %s" % str(err)
                conn_attempts = self._check_connection_attempts(conn_attempts, msg)
            # end exception handling
        # end query events forever

        return list()

    def _save_event_id_data(self):
        """
        Save an event Id to persistent storage.

        Next time the engine is started it will try to read the event id from
        this location to know at which event it should start processing.
        """
        event_id_file = self.settings_value()['event-journal-file'].expandvars()

        if not event_id_file:
            return
        # end bail out early

        for collection in self._plugin_collections:
            self._event_id_data[collection.path] = collection.state()
        # end gather plugin state

        if not self._event_id_data:
            self.log.warning('No state was found. Not saving to disk.')
        # end bail out if there is nothing to save

        try:
            fh = open(event_id_file, 'wb')
            pickle.dump(self._event_id_data, fh)
            fh.close()
        except OSError as err:
            # NOTE: it's not an immediate error if writes fail, as we have our state in-memory
            # However, we can't recover until this is fixed
            self.log.error("Can not write event id data to '%s.'", event_id_file, exc_info=True)
        # end handle errors
            

    def _check_connection_attempts(self, conn_attempts, msg):
        conn_attempts += 1
        config = self.settings_value().connection
        if conn_attempts == config.retries:
            self.log.error('Unable to connect to Shotgun (attempt %s of %s): %s', conn_attempts, config.retries, msg)
            conn_attempts = 0
            time.sleep(config['retry-every'].seconds)
        else:
            self.log.warning('Unable to connect to Shotgun (attempt %s of %s): %s', conn_attempts, config.retries, msg)
        # end 
        return conn_attempts
