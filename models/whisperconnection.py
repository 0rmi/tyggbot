import random
import requests
from queue import Queue
import threading
import json
import pymysql

import logging

log = logging.getLogger('tyggbot')


class Whisper:
    def __init__(self, target, message):
        self.target = target
        self.message = message


class WhisperConnection:
    def __init__(self, conn, name, oauth):
        self.conn = conn
        self.num_msgs_sent = 0
        self.name = name
        self.oauth = oauth

    def reduce_msgs_sent(self):
        self.num_msgs_sent -= 1


class WhisperConnectionManager:
    def __init__(self, reactor, tyggbot, target, message_limit, time_interval, num_of_conns=30):
        self.reactor = reactor
        self.tyggbot = tyggbot
        self.message_limit = message_limit
        self.time_interval = time_interval
        self.num_of_conns = num_of_conns

        self.connlist = []
        self.whispers = Queue()

        self.maintenance_lock = False

    def __contains__(self, connection):
        return connection in [c.conn for c in self.connlist]

    def start(self, accounts=[]):
        log.debug("Starting connection manager")
        try:
            # Update available group servers.
            # This will also be run at an interval to make sure it's up to date
            self.update_servers_list()
            self.reactor.execute_every(3600, self.update_servers_list)

            # Run the maintenance function every 4 seconds.
            # The maintenance function is responsible for reconnecting lost connections.
            self.reactor.execute_every(4, self.run_maintenance)

            # Fetch additional whisper accounts from the database
            self.tyggbot.sqlconn.ping()
            cursor = self.tyggbot.sqlconn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("SELECT `username`, `oauth` FROM `tb_whisper_account` WHERE `enabled`=1 ORDER BY RAND() LIMIT %s", self.num_of_conns)
            for row in cursor:
                accounts.append(row)

            # Start the connections.
            t = threading.Thread(target=self.start_connections, args=[accounts])
            t.daemon = True
            t.start()

            return True
        except:
            log.exception("WhisperConnectionManager: Unhandled exception")
            return False

    def start_connections(self, accounts):
        for account in accounts:
            newconn = self.make_new_connection(account['username'], account['oauth'])
            self.connlist.append(newconn)

        t = threading.Thread(target=self.whisper_sender)  # start a loop sending whispers in a thread
        t.daemon = True
        t.start()

    def quit(self):
        for connection in self.connlist:
            connection.conn.quit('bye')

    def update_servers_list(self):
            log.debug("Refreshing list of whisper servers")
            servers_list = json.loads(requests.get("http://tmi.twitch.tv/servers?cluster=group").text)
            self.servers_list = servers_list['servers']

    def whisper_sender(self):
        while True:
            whisp = self.whispers.get()
            username = whisp.target
            message = whisp.message

            i = 0
            while((not self.connlist[i].conn.is_connected()) or self.connlist[i].num_msgs_sent >= self.message_limit):
                i += 1  # find a usable connection
                if i >= len(self.connlist):
                    i = 0

            log.debug('Sending whisper: {0} {1}'.format(username, message))
            self.connlist[i].conn.privmsg('#jtv', '/w {0} {1}'.format(username, message))
            self.connlist[i].num_msgs_sent += 1
            self.connlist[i].conn.execute_delayed(self.time_interval, self.connlist[i].reduce_msgs_sent)

    def run_maintenance(self):
        if self.maintenance_lock:
            return
        
        self.maintenance_lock = True
        for connection in self.connlist:
            if not connection.conn.is_connected():
                connection.conn.close()
                self.connlist.remove(connection)
                newconn = self.make_new_connection(connection.name, connection.oauth)
                self.connlist.append(newconn)

        self.maintenance_lock = False

    def get_main_conn(self):
        for connection in self.connlist:
            if connection.conn.is_connected():
                return connection.conn

        log.error("No connection with is_connected() found in WhisperConnectionManager")

    def make_new_connection(self, name, oauth):
        server = random.choice(self.servers_list)
        ip, port = server.split(':')
        port = int(port)
        log.debug("Whispers: Connection to server {0}".format(server))

        newconn = self.reactor.server().connect(ip, port, name, oauth, name)
        newconn.cap('REQ', 'twitch.tv/commands')
        return WhisperConnection(newconn, name, oauth)

    def on_disconnect(self, conn):
        conn.reconnect()
        conn.cap('REQ', 'twitch.tv/commands')
        return

    def whisper(self, target, message):
        if not target:
            target = self.target
        self.whispers.put(Whisper(target, message))
