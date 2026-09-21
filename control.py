"""Locate the private control topic and keep all app messages inside it."""

from telethon import functions, utils


class ControlTopic:
    def __init__(self, client, peer, chat_id, topic_id):
        self.client = client
        self.peer = peer
        self.chat_id = chat_id
        self.topic_id = topic_id

    def contains(self, event):
        if event.chat_id != self.chat_id:
            return False
        reply = event.message.reply_to
        if reply is None:
            return False
        return (reply.reply_to_top_id or reply.reply_to_msg_id) == self.topic_id

    async def send(self, text):
        sent = None
        for start in range(0, len(text), 3900):
            sent = await self.client.send_message(
                self.peer, text[start:start + 3900], reply_to=self.topic_id,
                parse_mode=None, link_preview=False,
            )
        return sent


async def find_control_topic(client, group_name, topic_name):
    matches = []
    async for dialog in client.iter_dialogs():
        if dialog.name.casefold() == group_name.casefold():
            matches.append(dialog)
    if len(matches) != 1:
        raise RuntimeError(
            f"Группа «{group_name}» не найдена или её имя не уникально "
            f"(совпадений: {len(matches)})."
        )

    dialog = matches[0]
    peer = dialog.input_entity
    result = await client(functions.messages.GetForumTopicsRequest(
        peer=peer, q=topic_name, offset_date=None,
        offset_id=0, offset_topic=0, limit=100,
    ))
    topics = [
        topic for topic in result.topics
        if getattr(topic, "title", "").casefold() == topic_name.casefold()
    ]
    if len(topics) != 1:
        raise RuntimeError(
            f"Тема «{topic_name}» в группе «{group_name}» не найдена "
            f"или её имя не уникально (совпадений: {len(topics)})."
        )
    if topics[0].closed:
        raise RuntimeError(f"Тема «{topic_name}» закрыта для сообщений.")
    return ControlTopic(client, peer, utils.get_peer_id(dialog.entity), topics[0].id)
