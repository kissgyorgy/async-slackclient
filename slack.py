import re
import json
import asyncio
from functools import lru_cache
from urllib.parse import urlencode
import aiohttp


class Emoji:
    PLUS_ONE = ":+1:"
    POOP = ":poop:"
    JS = ":js:"
    EXCLAMATION = ":exclamation:"
    WHITE_CHECK_MARK = ":white_check_mark:"
    X = ":x:"


SLACK_API_URL = "https://slack.com/api"
SLACK_OAUTH_URL = "https://slack.com/oauth/authorize"


def escape(text):
    """Escape Slack special characters.
    See: https://api.slack.com/docs/message-formatting#how_to_escape_characters
    """
    rv = text.replace("<", "&lt;")
    rv = rv.replace(">", "&gt;")
    rv = rv.replace("&", "&amp;")
    return rv


def make_link(url, text):
    return f"<{url}|{text}>"


def parse_links(text):
    return re.findall(r"<(.*?)>", text.strip())


def make_attachment(color, author_name, author_link):
    return {"color": color, "author_name": author_name, "author_link": author_link}


class ApiError(Exception):
    """Exception in case a message for Slack failed"""


class AsyncApi:
    def __init__(self, token, session):
        self._token = token
        self._headers = {
            "Authorization": "Bearer " + token,
            # Slack needs a charset, otherwise it will send a warning in every response...
            "Content-Type": "application/json; charset=utf-8",
        }
        self._session = session

    def _error_message(self, res, method, body, payload):
        return (
            f"Error {res.status} during {method} {res.method} request: {body}\n"
            f"payload: {payload}"
        )

    async def _make_json_res(self, res, method, payload):
        json_res = await res.json()
        # https://api.slack.com/web#responses
        if not (200 <= res.status < 400) or "ok" not in json_res:
            # we couldn't even parse the response into a proper json format
            res_body = await res.text()
            error_message = self._error_message(res, method, res_body, payload)
            raise ApiError(error_message)

        # "ok" in json_res only mean we got a properly formatted json response,
        # but it still could contain an error when it's value is false
        if not json_res["ok"]:
            print(self._error_message(res, method, json_res, payload))

        return json_res

    async def _get(self, method, params=None):
        print("Request", method, params)
        url = f"{SLACK_API_URL}/{method}"
        async with self._session.get(url, params=params, headers=self._headers) as res:
            return await self._make_json_res(res, method, params)

    async def _get_all(self, method, field, params):
        rv = []
        while True:
            json_res = await self._get(method, params)
            if not json_res["ok"]:
                return
            rv.extend(json_res[field])
            next_cursor = json_res.get("response_metadata", {}).get("next_cursor")
            print("Next cursor:", next_cursor or type(next_cursor))
            if not next_cursor:
                return rv
            params["cursor"] = next_cursor

    async def _post(self, method, payload):
        print("Posting to", method, payload)
        url = f"{SLACK_API_URL}/{method}"
        async with self._session.post(url, headers=self._headers, json=payload) as res:
            return await self._make_json_res(res, method, payload)

    async def add_reaction(self, channel, ts, reaction_name):
        payload = {"channel": channel, "timestamp": ts, "name": reaction_name}
        return await self._post("reactions.add", payload)

    async def get_permalink(self, channel_id, ts):
        payload = {"channel": channel_id, "message_ts": ts}
        res = await self._get("chat.getPermalink", payload)
        return res["permalink"] if res["ok"] else None

    async def list_all_channels(self):
        params = {"types": "public_channel,private_channel"}
        return await self._get_all("conversations.list", "channels", params)

    async def get_channel_id(self, channel_name):
        name = channel_name.lstrip("#")
        # TODO: make it async for
        for channel in await self.list_all_channels():
            if channel["name"] == name:
                return channel["id"]

    async def post_message(self, channel_id, text, attachments, thread_ts=None):
        # as_user is needed, so direct messages can be deleted.
        # if DMs are sent to the user without as_user: True, they appear
        # as if slackbot sent them and there will be no channel which
        # can be referenced later to delete the sent messages
        return await self._post(
            "chat.postMessage",
            {
                "channel": channel_id,
                "text": text,
                "attachments": attachments,
                "as_user": True,
                "thread_ts": thread_ts,
            },
        )

    async def delete_message(self, channel_id, ts):
        return await self._post("chat.delete", {"channel": channel_id, "ts": ts})

    async def user_info(self, user_id):
        return await self._get("users.info", {"user": user_id})

    async def revoke_token(self):
        method = "auth.revoke"
        url = f"{SLACK_API_URL}/{method}"
        # this method doesn't accept JSON body
        async with self._session.post(url, {"token": self._token}) as res:
            return await self._make_json_res(res, method, {"token": "XXXXXXXXXX"})

    async def channel_info(self, channel_id):
        return await self._get("channels.info", {"channel": channel_id})

    async def rtm_connect(self, *, retry=False):
        return await self._make_rtm_api("rtm.connect", retry)

    async def rtm_start(self, *, retry=False):
        return await self._make_rtm_api("rtm.start", retry)

    async def _make_rtm_api(self, method, retry):
        while True:
            res = await self._get(method)
            if res["ok"]:
                break

            message = f"Couldn't connect to RTM api: {res['error']}"
            if retry:
                print(message + ", trying again...")
                await asyncio.sleep(5)
            else:
                raise ApiError(message)

        print("Connected to RTM api:", res)
        ws = await self._session.ws_connect(res["url"])
        return _RealtimeApi(res["self"]["id"], ws)


class MsgType:
    HELLO = "hello"
    TYPING = "typing"
    USER_TYPING = "user_typing"
    MESSAGE = "message"
    DESKTOP_NOTIFICATION = "desktop_notification"
    GOODBYE = "goodbye"


class MsgSubType:
    MESSAGE_CHANGED = "message_changed"
    MESSAGE_DELETED = "message_deleted"


class _RealtimeApi:
    def __init__(self, bot_id, ws):
        self._bot_id = bot_id
        self._ws = ws

    @property
    def bot_id(self):
        return self._bot_id

    @property
    def bot_mention(self):
        return f"<@{self.bot_id}>"

    async def got_hello(self):
        msg = await self.wait_messages().__anext__()
        return msg["type"] == MsgType.HELLO

    async def wait_messages(self):
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                message_json = json.loads(msg.data)
                if message_json.get("type") == MsgType.GOODBYE:
                    await self.close()
                    break

                yield message_json

            elif msg.type == aiohttp.WSMsgType.ERROR:
                print("An unknown error occured in the connection, exiting...")
                break

    async def close(self):
        # already closed
        if self._ws is None:
            return
        await self._ws.close()
        self._ws = None

    async def send_typing_indicator(self, channel_id):
        # FIXME: should be a real id
        message = {"id": 1, "type": MsgType.TYPING, "channel": channel_id}
        await self._ws.send_json(message)

    async def reply_in_thread(self, channel_id, ts, text):
        # FIXME: should be a real id
        message = {
            "id": 2,
            "type": MsgType.MESSAGE,
            "channel": channel_id,
            "text": text,
            "thread_ts": ts,
        }
        await self._ws.send_json(message)


class Api(AsyncApi):
    def __init__(self, token):
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            # when running in a thread, get_event_loop doesn't create another one
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

        self._session = aiohttp.ClientSession(loop=self._loop)
        super().__init__(token, self._session)

    @lru_cache(maxsize=None)
    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name.startswith("_") or not asyncio.iscoroutinefunction(attr):
            return attr

        def call_sync(*args, **kwargs):
            coro = attr(*args, **kwargs)
            return self._loop.run_until_complete(coro)

        return call_sync


class App:
    SCOPE = "commands,bot"

    def __init__(self, client_id, client_secret, redirect_uri):
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def request_oauth_token(self, code):
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self._request_oauth_token(code))

    async def _request_oauth_token(self, code):
        # documentation: https://api.slack.com/methods/oauth.access
        url = SLACK_API_URL + "/oauth.access"
        payload = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self._redirect_uri,
            "code": code,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=payload) as res:
                # example in slack_messages/oauth.access.json
                return await res.json()

    def make_button_url(self, state):
        params = {
            "scope": self.SCOPE,
            "client_id": self._client_id,
            "state": state,
            "redirect_uri": self._redirect_uri,
        }
        encoded_params = urlencode(params, safe=",")
        return f"{SLACK_OAUTH_URL}?{encoded_params}"
