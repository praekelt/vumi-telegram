import json

from treq.client import HTTPClient

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web import http
from twisted.web.client import Agent

from vumi.transports.httprpc.httprpc import HttpRpcTransport
from vumi.persist.txredis_manager import TxRedisManager
from vumi.config import ConfigText, ConfigUrl, ConfigDict, ConfigInt


class TelegramTransportConfig(HttpRpcTransport.CONFIG_CLASS):
    bot_username = ConfigText(
        'The username of our Telegram bot', static=True, required=True,
    )
    bot_token = ConfigText(
        "Our bot's unique token to access the Telegram API",
        static=True, required=True,
    )
    outbound_url = ConfigUrl(
        'The URL our bot should make requests to', static=True,
        default='https://api.telegram.org/bot', required=False,
    )
    inbound_url = ConfigUrl(
        'The URL our transport will listen on for Telegram updates',
        static=True, required=True,
    )
    redis_manager = ConfigDict(
        'Parameters to connect to Redis with', default={}, static=True,
        required=False,
    )
    update_lifetime = ConfigInt(
        'How long we store update_ids in Redis (in seconds)',
        # Default: 24 hours (how long Telegram stores updates on their servers)
        default=(60 * 60 * 24), static=True, required=False,
    )


class TelegramTransport(HttpRpcTransport):
    """
    Telegram transport for Vumi and Junebug.
    See the Telegram Bot API at https://core.telegram.org/bots/api.
    """
    transport_type = 'telegram'
    transport_name = 'telegram_transport'

    CONFIG_CLASS = TelegramTransportConfig

    # Telegram usernames are human-readable strings that identify users
    TELEGRAM_USERNAME = 'telegram_username'
    # Telegram ids are integers that identify users to the Telegram API
    TELEGRAM_ID = 'telegram_id'

    media_api_path = {
        'photo': 'sendPhoto',
        'document': 'sendDocument',
        'contact': 'sendContact',
        'venue': 'sendVenue',
        'location': 'sendLocation',
    }

    @classmethod
    def agent_factory(cls):
        """
        For swapping out the Agent we use in tests.
        """
        return Agent(reactor)

    @inlineCallbacks
    def setup_transport(self):
        yield super(TelegramTransport, self).setup_transport()
        yield self.add_status_starting()

        config = self.get_static_config()
        self.api_url = '%s%s' % (config.outbound_url.geturl().rstrip('/'),
                                 config.bot_token)
        self.inbound_url = config.inbound_url.geturl()
        self.bot_username = config.bot_username
        self.redis = yield TxRedisManager.from_config(config.redis_manager)

        yield self.setup_webhook()
        yield self.add_status_started()

    @inlineCallbacks
    def setup_webhook(self):
        """
        Sets up a webhook to receive updates from Telegram.
        """
        url = self.get_outbound_url('setWebhook')
        http_client = HTTPClient(self.agent_factory())

        r = yield http_client.post(
            url=url,
            data=json.dumps({'url': self.inbound_url}),
            headers={'Content-Type': ['application/json']},
            allow_redirects=False,
        )

        validate = yield self.validate_outbound(r)
        if validate['success']:
            self.log.info('Webhook set up on %s' % self.inbound_url)
            yield self.add_status_good_webhook()
        else:
            self.log.warning('Webhook setup failed: %s' % validate['message'])
            yield self.add_status_bad_webhook(
                status_type=validate['status'],
                message='Webhook setup failed: %s' % validate['message'],
                details=validate['details'],
            )

    def add_status_good_webhook(self):
        return self.add_status(
            status='ok',
            component='telegram_webhook',
            type='webhook_setup_success',
            message='Webhook setup successful',
            details={'webhook_url': self.inbound_url},
        )

    def add_status_bad_webhook(self, status_type, message, details):
        return self.add_status(
            status='down',
            component='telegram_webhook',
            type=status_type,
            message=message,
            details=details,
        )

    def get_outbound_url(self, path):
        return '%s/%s' % (self.api_url, path)

    def log_inbound(self, update_type, user):
        """
        Log the receipt of an inbound update.
        """
        if user.get('username') is None:
            user = user['id']
        else:
            user = user['username']
        self.log.info(
            'TelegramTransport receiving %s from %s to %s' % (
                update_type, user, self.bot_username))

    @inlineCallbacks
    def handle_raw_inbound_message(self, message_id, request):
        content = yield request.content.read()
        try:
            update = json.loads(content)
        except ValueError as e:
            self.log.warning('Inbound update in unexpected format: %s' % e)
            yield self.add_status_bad_inbound(
                status_type='unexpected_update_format',
                message='Inbound update in unexpected format',
                details={'error': e.message, 'req_content': content},
            )
            request.setResponseCode(http.BAD_REQUEST)
            request.finish()
            return

        # Do not process duplicate requests
        update_id = update['update_id']
        is_duplicate = yield self.is_duplicate(update_id)
        if is_duplicate:
            self.log.info('Received a duplicate update: %s' % update_id)
            request.finish()
            return
        yield self.mark_as_seen(update_id)

        # Handle callback queries separately
        if 'callback_query' in update:
            yield self.handle_inbound_callback_query(
                message_id=message_id,
                callback_query=update['callback_query'],
            )
            request.finish()
            return

        # Handle inline queries separately
        if 'inline_query' in update:
            yield self.handle_inbound_inline_query(
                message_id=message_id,
                inline_query=update['inline_query'],
            )
            request.finish()
            return

        # Ignore updates that do not contain message objects
        if 'message' not in update:
            self.log.info('Inbound update does not contain a message')
            request.finish()
            return

        # Ignore messages that aren't text messages
        message = update['message']
        if 'text' not in message:
            self.log.info('Inbound message is not a text message')
            request.finish()
            return

        message = self.translate_inbound_message(update['message'])
        self.log_inbound('message', {
            'id': message['from_addr'],
            'username': message['telegram_username'],
        })

        yield self.publish_message(
            message_id=message_id,
            content=message['content'],
            to_addr=self.bot_username,
            to_addr_type=self.TELEGRAM_USERNAME,
            from_addr=message['from_addr'],
            from_addr_type=self.TELEGRAM_ID,
            transport_type=self.transport_type,
            transport_name=self.transport_name,
            helper_metadata={'telegram': {
                'telegram_username': message['telegram_username'],
            }},
            transport_metadata={
                'telegram_msg_id': message['telegram_msg_id'],
                'telegram_username': message['telegram_username'],
            },
        )

        yield self.add_status(
            status='ok',
            component='telegram_inbound',
            type='good_inbound',
            message='Good inbound request',
        )
        request.finish()

    def get_update_id_key(self, update_id):
        return 'update_id:%s' % update_id

    @inlineCallbacks
    def is_duplicate(self, update_id):
        """
        Checks to see if an inbound update has already been processed.
        """
        exists = yield self.redis.exists(self.get_update_id_key(update_id))
        returnValue(exists)

    @inlineCallbacks
    def mark_as_seen(self, update_id):
        """
        Adds an update_id to a list of update_ids already processed.
        """
        config = self.get_static_config()
        key = self.get_update_id_key(update_id)
        yield self.redis.setex(key, 1, config.update_lifetime)

    def add_status_bad_inbound(self, status_type, message, details):
        return self.add_status(
            status='down',
            component='telegram_inbound',
            type=status_type,
            message=message,
            details=details,
        )

    @inlineCallbacks
    def handle_inbound_callback_query(self, message_id, callback_query):
        """
        Handles an inbound callback query, fired when a user makes a selection
        on an inline keyboard.
        """
        self.log_inbound('callback query', callback_query['from'])

        metadata = {
           'type': 'callback_query',
           'reply': callback_query.get('data'),
           'details': {'callback_query_id': callback_query['id']},
        }
        telegram_username = callback_query['from'].get('username')
        if telegram_username:
            metadata['telegram_username'] = telegram_username

        yield self.publish_message(
            message_id=message_id,
            content='',
            to_addr=self.bot_username,
            to_addr_type=self.TELEGRAM_USERNAME,
            from_addr=callback_query['from']['id'],
            from_addr_type=self.TELEGRAM_ID,
            transport_type=self.transport_type,
            transport_name=self.transport_name,
            helper_metadata={'telegram': metadata},
            transport_metadata=metadata,
        )

        yield self.add_status(
            status='ok',
            component='telegram_inbound',
            type='good_inbound',
            message='Good inbound request',
        )

    @inlineCallbacks
    def handle_inbound_inline_query(self, message_id, inline_query):
        """
        Handles an inbound inline query from a Telegram user.
        """
        self.log_inbound('inline query', inline_query['from'])


        metadata = {
           'type': 'callback_query',
           'reply': inline_query['query'],
           'details': {'callback_query_id': callback_query['id']},
        }
        telegram_username = callback_query['from'].get('username')
        if telegram_username:
            metadata['telegram_username'] = telegram_username

        yield self.publish_message(
            message_id=message_id,
            content='',
            to_addr=self.bot_username,
            to_addr_type=self.TELEGRAM_USERNAME,
            from_addr=inline_query['from']['id'],
            from_addr_type=self.TELEGRAM_ID,
            transport_type=self.transport_type,
            transport_name=self.transport_name,
            helper_metadata={'telegram': metadata},
            transport_metadata=metadata,
        )

        yield self.add_status(
            status='ok',
            component='telegram_inbound',
            type='good_inbound',
            message='Good inbound request',
        )

    def translate_inbound_message(self, message):
        """
        Translates inbound Telegram message into Vumi's default format.
        """
        telegram_msg_id = message['message_id']
        content = message['text']

        # Messages sent over channels do not contain a 'from' field - in that
        # case, we want the channel's chat id
        if 'from' in message:
            from_addr = message['from']['id']
            telegram_username = message['from'].get('username')
        else:
            from_addr = message['chat']['id']
            telegram_username = message['chat'].get('username')

        return {
            'telegram_msg_id': telegram_msg_id,
            'content': content,
            'from_addr': from_addr,
            'telegram_username': telegram_username,
        }

    @inlineCallbacks
    def handle_outbound_message(self, message):
        message_id = message['message_id']
        metadata = message['helper_metadata'].get('telegram')

        # Handle replies to inline queries separately
        if message['transport_metadata'].get('type') == 'inline_query':
            yield self.handle_outbound_inline_query(message_id, message)
            return

        # Handle replies to callback queries separately
        if message['transport_metadata'].get('type') == 'callback_query':
            yield self.handle_outbound_callback_query(message_id, message)
            return

        # Handle messages with media attachments
        if metadata is not None:
            attachment = metadata.get('attachment')
            if attachment is not None:
                yield self.handle_outbound_media_message(message_id, message)
                return

        outbound_msg = {
            'chat_id': message['to_addr'],
            'text': message['content'],
        }

        # Handle direct replies
        if message.get('in_reply_to') is not None:
            telegram_msg_id = message['transport_metadata'].get('telegram_msg_id')
            if telegram_msg_id:
                outbound_msg.update({'reply_to_message_id': telegram_msg_id})

        # Handle message formatting options (pass if none are provided)
        if metadata is not None:
            outbound_msg.update(metadata)

        url = self.get_outbound_url('sendMessage')
        http_client = HTTPClient(self.agent_factory())

        r = yield http_client.post(
            url=url,
            data=json.dumps(outbound_msg),
            headers={'Content-Type': ['application/json']},
            allow_redirects=False,
        )

        validate = yield self.validate_outbound(r)
        if validate['success']:
            yield self.outbound_success(message_id)
        else:
            yield self.outbound_failure(
                message_id=message_id,
                message='Message not sent: %s' % validate['message'],
                status_type=validate['status'],
                details=validate['details'],
            )

    @inlineCallbacks
    def handle_outbound_media_message(self, message_id, message):
        """
        Handles an outbound message that contains embedded media.
        """
        att = message['helper_metadata']['telegram']['attachment']
        try:
            url = self.get_outbound_url(self.media_api_path[att['type']])
        except KeyError:
            self.log.info('Unsupported attachment type: %s' % att.get('type'))
            return

        http_client = HTTPClient(self.agent_factory())
        params = {
            'chat_id': message['to_addr'],
        }
        params.update(att)
        del params['type']

        # Handle direct replies
        if message['in_reply_to'] is not None:
            telegram_msg_id = message['transport_metadata']['telegram_msg_id']
            params.update({'reply_to_message_id': telegram_msg_id})

        r = yield http_client.post(
            url=url,
            data=json.dumps(params),
            headers={'Content-Type': ['application/json']},
            allow_redirects=False,
        )

        validate = yield self.validate_outbound(r)
        if validate['success']:
            yield self.outbound_success(message_id)
            self.add_status(
                status='ok',
                component='telegram_outbound_media_message',
                type='good_outbound_media_message',
                message='Outbound request successful',
            )
        else:
            yield self.outbound_failure(
                message_id=message_id,
                message='Media message not sent: %s'
                        % validate['message'],
                status_type=validate['status'],
                details=validate['details'],
            )

    @inlineCallbacks
    def handle_outbound_callback_query(self, message_id, message):
        """
        Handles replies to callback queries from inline keyboards. This method
        must be called after receiving a callback query (even if we do not
        send a reply) to prevent the user being stuck with a progress bar.
        """
        url = self.get_outbound_url('answerCallbackQuery')
        http_client = HTTPClient(self.agent_factory())

        qry_id = message['transport_metadata']['details']['callback_query_id']

        params = {
            'callback_query_id': qry_id,
            'text': message['content'],
        }
        params.update(message['helper_metadata']['telegram'].get('details'))

        r = yield http_client.post(
            url=url,
            data=json.dumps(params),
            headers={'Content-Type': ['application/json']},
            allow_redirects=False,
        )

        validate = yield self.validate_outbound(r)
        if validate['success']:
            yield self.outbound_success(message_id)
            self.add_status(
                status='ok',
                component='telegram_callback_query_reply',
                type='good_callback_query_reply',
                message='Outbound request successful',
            )
        else:
            validate['details'].update({'callback_query_id': qry_id})
            yield self.outbound_failure(
                message_id=message_id,
                message='Callback query reply not sent: %s'
                        % validate['message'],
                status_type=validate['status'],
                details=validate['details'],
            )

    @inlineCallbacks
    def handle_outbound_inline_query(self, message_id, message):
        """
        Handles replies to inline queries. We rely on the application worker to
        generate the result(s).
        """
        url = self.get_outbound_url('answerInlineQuery')
        http_client = HTTPClient(self.agent_factory())

        query_id = message['transport_metadata']['details']['inline_query_id']

        try:
            outbound_query_answer = {
                'inline_query_id': query_id,
                'results': message['helper_metadata']['telegram']['results'],
            }

        # Don't break if outbound messages are not in the correct format
        except KeyError:
            self.log.info(
                'Inline query reply not sent: results field missing')
            self.publish_nack(
                message_id,
                'Inline query reply not sent: results field missing',
            )
            self.add_status(
                status='down',
                component='telegram_inline_query_reply',
                type='bad_inline_query_reply',
                message='Inline query reply not sent: results field missing',
                details={
                    'error': "Transport received an outbound inline query "
                             "reply that did not contain any results. Check "
                             "that your application is configured to reply to "
                             "inline queries. If you're not supporting inline "
                             "queries, you should disable your bot's inline "
                             "mode.",
                },
            )
            return

        r = yield http_client.post(
            url=url,
            data=json.dumps(outbound_query_answer),
            headers={'Content-Type': ['application/json']},
            allow_redirects=False,
        )

        validate = yield self.validate_outbound(r)
        if validate['success']:
            yield self.outbound_success(message_id)
            self.add_status(
                status='ok',
                component='telegram_inline_query_reply',
                type='good_inline_query_reply',
                message='Outbound request successful',
            )
        else:
            validate['details'].update({'inline_query_id': query_id})
            yield self.outbound_failure(
                message_id=message_id,
                message='Inline query reply not sent: %s' %
                        validate['message'],
                status_type=validate['status'],
                details=validate['details'],
            )

    @inlineCallbacks
    def validate_outbound(self, response):
        """
        Checks whether a request to Telegram's API was successful, and returns
        relevant information for publishing nacks / statuses if not.
        """
        # If our request is redirected, it likely means our bot token is in
        # an invalid format
        if response.code == http.FOUND:
            returnValue({
                'success': False,
                'message': 'request redirected',
                'status': 'request_redirected',
                'details': {
                    'error': 'Unexpected redirect',
                    'res_code': http.FOUND,
                },
            })

        try:
            res = yield response.json()
        except ValueError as e:
            content = yield response.content()
            returnValue({
                'success': False,
                'message': 'unexpected response format',
                'status': 'unexpected_response_format',
                'details': {
                    'error': e.message,
                    'res_code': response.code,
                    'res_body': content,
                },
            })

        if response.code == http.OK and res['ok']:
            returnValue({'success': True})
        else:
            returnValue({
                'success': False,
                'message': 'bad response from Telegram',
                'status': 'bad_response',
                'details': {
                    'error': res['description'],
                    'res_code': response.code,
                },
            })

    @inlineCallbacks
    def outbound_failure(self, status_type, message_id, message, details):
        yield self.publish_nack(message_id, message)
        yield self.add_status_bad_outbound(status_type, message, details)

    @inlineCallbacks
    def outbound_success(self, message_id):
        yield self.publish_ack(message_id, message_id)
        yield self.add_status_good_outbound()

    def add_status_bad_outbound(self, status_type, message, details):
        return self.add_status(
            status='down',
            component='telegram_outbound',
            type=status_type,
            message=message,
            details=details,
        )

    def add_status_good_outbound(self):
        return self.add_status(
            status='ok',
            component='telegram_outbound',
            type='good_outbound_request',
            message='Outbound request successful',
        )

    def add_status_starting(self):
        return self.add_status(
            status='down',
            component='telegram_setup',
            type='starting',
            message='Telegram transport starting...',
        )

    def add_status_started(self):
        return self.add_status(
            status='ok',
            component='telegram_setup',
            type='started',
            message='Telegram transport set up',
        )
