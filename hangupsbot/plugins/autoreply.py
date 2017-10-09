import asyncio, re, logging, json, random
from random import randint

import hangups

import plugins


logger = logging.getLogger(__name__)


def _initialise(bot):
    plugins.register_handler(_handle_autoreply, type="message")
    plugins.register_handler(_handle_autoreply, type="membership")
    plugins.register_admin_command(["autoreply"])


def _handle_autoreply(bot, event, command):
    config_autoreplies = bot.get_config_suboption(event.conv.id_, 'autoreplies_enabled')
    tagged_autoreplies = "autoreplies-enable" in bot.tags.useractive(event.user_id.chat_id, event.conv.id_)

    if not (config_autoreplies or tagged_autoreplies):
        return

    if "autoreplies-disable" in bot.tags.useractive(event.user_id.chat_id, event.conv.id_):
        logger.debug("explicitly disabled by tag for {} {}".format(event.user_id.chat_id, event.conv.id_))
        return

    """Handle autoreplies to keywords in messages"""

    if isinstance(event.conv_event, hangups.ChatMessageEvent):
        event_type = "MESSAGE"
    elif isinstance(event.conv_event, hangups.MembershipChangeEvent):
        if event.conv_event.type_ == hangups.MembershipChangeType.JOIN:
            event_type = "JOIN"
        else:
            event_type = "LEAVE"
    elif isinstance(event.conv_event, hangups.RenameEvent):
        event_type = "RENAME"
    else:
        raise RuntimeError("unhandled event type")

    # get_config_suboption returns the convo specific autoreply settings. If none set, it returns the global settings.
    autoreplies_list = bot.get_config_suboption(event.conv_id, 'autoreplies')

    """option to merge per-conversation and global autoreplies, by:
    * tagging a conversation with "autoreplies-merge" explicitly or by wildcard conv tag
    * setting global config key: autoreplies.merge = true
    note: you must also define the appropriate autoreply keys for a specific conversation
    (by default per-conversation autoreplies replaces global autoreplies settings completely)"""

    tagged_autoreplies_merge = "autoreplies-merge" in bot.tags.convactive(event.conv_id)
    config_autoreplies_merge = bot.get_config_option('autoreplies.merge') or False

    if tagged_autoreplies_merge or config_autoreplies_merge:

        # load any global settings as well
        autoreplies_list_global = bot.get_config_option('autoreplies')

        # If the global settings loaded from get_config_suboption then we now have them twice and don't need them, so can be ignored.
        if autoreplies_list_global and (set([ frozenset([
                frozenset(x) if isinstance(x, list) else x,
                frozenset(y) if isinstance(y, list) else y ]) for x, y in autoreplies_list_global ])
                != set([ frozenset([ frozenset(x) if isinstance(x, list) else x,
                         frozenset(y) if isinstance(y, list) else y ]) for x, y in autoreplies_list ])):

            add_to_autoreplies = []

            # If the two are different, then iterate through each of the triggers in the global list and if they
            # match any of the triggers in the convo list then discard them.
            # Any remaining at the end of the loop are added to the first list to form a consolidated list
            # of per-convo and global triggers & replies, with per-convo taking precident.

            # Loop through list of global triggers e.g. ["hi","hello","hey"],["baf","BAF"].
            for kwds_gbl, sentences_gbl in autoreplies_list_global:
                overlap = False
                for kwds_lcl, sentences_lcl in autoreplies_list:
                    if type(kwds_gbl) is type(kwds_lcl) is list and (set(kwds_gbl) & set(kwds_lcl)):
                        overlap = True
                        break
                if not overlap:
                    add_to_autoreplies.extend( [[kwds_gbl, sentences_gbl]] )

            # Extend original list with non-disgarded entries.
            autoreplies_list.extend( add_to_autoreplies )

    ## If text contains a key phrase, find it and end all comms with it for the next 4-7 times
    ending_phrases = ['\"done\"', '\"okay\"']
    for phrase in ending_phrases:
        if event.text.lower().find(phrase) != -1:
            bot.keyword_responses[event.conv_id] = {'keyword':phrase, "counter":randint(5, 8)}

    ## If text contains parenth with numbers in it, handle differently ##

    if re.findall('\d+.*?(?:\.|\))', event.text) and not re.findall('\d+\w+', event.text):
        logger.info("Entering Multi-Line answer workflow")
        message = _multi_number_answer(event.text, autoreplies_list)
        yield from asyncio.sleep(30)
        yield from send_reply(bot, event, message)

    elif autoreplies_list:
        for kwds, sentences in autoreplies_list:
            if isinstance(sentences, list):
                message = random.choice(sentences)
            else:
                message = sentences

            if isinstance(kwds, list):
                for kw in kwds:
                    if _words_in_text(kw, event.text) or kw == "*":
                        logger.info("MATCHED CHAT: {}".format(kw))
                        event.anything_sent = 1
                        yield from asyncio.sleep(randint(6,15))
                        yield from send_reply(bot, event, message)
                        break

                    elif kw == 'no_match_but_must_reply_with_keyword' and (event.conv_id in bot.keyword_responses) and bot.keyword_responses[event.conv_id]["counter"]>0 and event.anything_sent==0:
                        bot.keyword_responses[event.conv_id]["counter"] -= 1
                        yield from asyncio.sleep(randint(6,15))
                        yield from send_reply(bot, event, message)
                        break

            elif event_type == kwds:
                logger.info("MATCHED EVENT: {}".format(kwds))
                yield from send_reply(bot, event, message)


@asyncio.coroutine
def send_reply(bot, event, message):
    values = { "event": event,
               "conv_title": bot.conversations.get_name( event.conv,
                                                         fallback_string=_("Unidentified Conversation") )}

    if "participant_ids" in dir(event.conv_event):
        values["participants"] = [ event.conv.get_user(user_id)
                                   for user_id in event.conv_event.participant_ids ]
        values["participants_namelist"] = ", ".join([ u.full_name for u in values["participants"] ])

    # tldr plugin integration: inject current conversation tldr text into auto-reply
    # if '{tldr}' in message:
    #     args = {'conv_id': event.conv_id, 'params': ''}
    #     try:
    #         values["tldr"] = bot.call_shared("plugin_tldr_shared", bot, args)
    #     except KeyError:
    #         values["tldr"] = "**[TLDR UNAVAILABLE]**" # prevents exception
    #         logger.warning("tldr plugin is not loaded")
    #         pass

    envelopes = []

    if (event.conv_id in bot.keyword_responses) and bot.keyword_responses[event.conv_id]["counter"] > 0:
        keyword = bot.keyword_responses[event.conv_id]["keyword"]
        envelopes.append((event.conv, message.format(**values) + '\n ' + keyword))
        bot.keyword_responses[event.conv_id]["counter"] -= 1
    else:
        envelopes.append((event.conv, message.format(**values)))


    # if message.startswith(("ONE_TO_ONE:", "HOST_ONE_TO_ONE")):
    #     message = message[message.index(":")+1:].strip()
    #     target_conv = yield from bot.get_1to1(event.user.id_.chat_id)
    #     if not target_conv:
    #         logger.error("1-to-1 unavailable for {} ({})".format( event.user.full_name,
    #                                                               event.user.id_.chat_id ))
    #         return False
    #     envelopes.append((target_conv, message.format(**values)))
    #
    # elif message.startswith("GUEST_ONE_TO_ONE:"):
    #     message = message[message.index(":")+1:].strip()
    #     for guest in values["participants"]:
    #         target_conv = yield from bot.get_1to1(guest.id_.chat_id)
    #         if not target_conv:
    #             logger.error("1-to-1 unavailable for {} ({})".format( guest.full_name,
    #                                                                   guest.id_.chat_id ))
    #             return False
    #         values["guest"] = guest # add the guest as extra info
    #         envelopes.append((target_conv, message.format(**values)))
    #
    # else:
    #
    #     envelopes.append((event.conv, message.format(**values)))
    #
    for send in envelopes:
        conv_target, message = send

        try:
            image_id = yield from bot.call_shared( 'image_validate_and_upload_single',
                                                    message,
                                                    reject_googleusercontent=False )
        except KeyError:
            logger.warning("image plugin not loaded - using in-built fallback")
            image_id = yield from image_validate_and_upload_single(message, bot)

        if image_id:
            yield from bot.coro_send_message(conv_target, None, image_id=image_id)
        else:
            yield from bot.coro_send_message(conv_target, message)

    return True


def _words_in_text(word, text):
    """Return True if word is in text"""

    if word.startswith("regex:"):
        word = word[6:]
    else:
        word = re.escape(word)

    regexword = "(?<!\w)" + word + "(?!\w)"

    return True if re.search(regexword, text, re.IGNORECASE) else False

def _multi_number_answer(text, replies_list):
    """Handle common practice in scam interviews of asking numbered questions
    to the candidate.

    For example:
    1) What type of jobs have you had and How did you get your previous jobs?
    (2) What were your main responsibilities?
    ( 3 ) Have you been in the military before? and Are you currently employed?
    """

    text = text.replace('\n', ' ').replace('\r', '')
    number_list = re.findall('\d+.*?(?:\.|\))', text)
    question_list = re.split('\d+.*?(?:\.|\))', text)
    clean_question_list = [ x.replace('(', '').replace(')', '').lstrip('.').lstrip(',').strip() for x in question_list if x]
    clean_question_list = [x for x in clean_question_list if x]

    answer_list = []

    ## Handle cases where lists are not cleaned properly
    ## Make the number and question list equal size
    if len(clean_question_list) != len(number_list):
        clean_question_list = clean_question_list[:len(number_list)]

    counter = len(number_list)

    while counter>0:
        for kwds, sentences in replies_list:
            if isinstance(sentences, list):
                message = random.choice(sentences)
            else:
                message = sentences

            if isinstance(kwds, list):
                for kw in kwds:
                    for question in clean_question_list:
                        if _words_in_text(kw, question):
                            answer_list.append(number_list[0] + ' ' + message + ' \n ')

                            if clean_question_list:
                                del clean_question_list[0]

                            if number_list:
                                del number_list[0]

                        counter -= 1

    return ''.join(answer_list)

def autoreply(bot, event, cmd=None, *args):
    """adds or removes an autoreply.
    Format:
    /bot autoreply add [["question1","question2"],"answer"] // add an autoreply
    /bot autoreply remove [["question"],"answer"] // remove an autoreply
    /bot autoreply // view all autoreplies
    """

    path = ["autoreplies"]
    argument = " ".join(args)
    html = ""
    value = bot.config.get_by_path(path)

    if cmd == 'add':
        if isinstance(value, list):
            value.append(json.loads(argument))
            bot.config.set_by_path(path, value)
            bot.config.save()
        else:
            html = "Append failed on non-list"
    elif cmd == 'remove':
        if isinstance(value, list):
            value.remove(json.loads(argument))
            bot.config.set_by_path(path, value)
            bot.config.save()
        else:
            html = "Remove failed on non-list"

    # Reload the config
    bot.config.load()

    if html == "":
        value = bot.config.get_by_path(path)
        html = "<b>Autoreply config:</b> <br /> {}".format(value)

    yield from bot.coro_send_message(event.conv_id, html)


"""FALLBACK CODE FOR IMAGE LINK VALIDATION AND UPLOAD
* CODE IS DEPRECATED - DO NOT CHANGE OR ALTER
* CODE MAY BE REMOVED AT ANY TIME
* FOR FUTURE-PROOFING, INCLUDE [image] PLUGIN IN YOUR CONFIG.JSON
"""

import aiohttp, io, os, re

def image_validate_link(image_uri, reject_googleusercontent=True):
    """
    validate and possibly mangle supplied image link
    returns False, if not an image link
            <string image uri>
    """

    if " " in image_uri:
        """immediately reject anything with non url-encoded spaces (%20)"""
        return False

    probable_image_link = False

    image_uri_lower = image_uri.lower()

    if re.match("^(https?://)?([a-z0-9.]*?\.)?imgur.com/", image_uri_lower, re.IGNORECASE):
        """imgur links can be supplied with/without protocol and extension"""
        probable_image_link = True

    elif image_uri_lower.startswith(("http://", "https://", "//")) and image_uri_lower.endswith((".png", ".gif", ".gifv", ".jpg", ".jpeg")):
        """other image links must have protocol and end with valid extension"""
        probable_image_link = True

    if probable_image_link and reject_googleusercontent and ".googleusercontent." in image_uri_lower:
        """reject links posted by google to prevent endless attachment loop"""
        logger.debug("rejected link {} with googleusercontent".format(image_uri))
        return False

    if probable_image_link:

        """special handler for imgur links"""
        if "imgur.com" in image_uri:
            if not image_uri.endswith((".jpg", ".gif", "gifv", "webm", "png")):
                image_uri = image_uri + ".gif"
            image_uri = "https://i.imgur.com/" + os.path.basename(image_uri)

            """imgur wraps animations in player, force the actual image resource"""
            image_uri = image_uri.replace(".webm",".gif")
            image_uri = image_uri.replace(".gifv",".gif")

        logger.debug('{} seems to be a valid image link'.format(image_uri))

        return image_uri

    return False

@asyncio.coroutine
def image_upload_single(image_uri, bot):
    logger.info("getting {}".format(image_uri))
    filename = os.path.basename(image_uri)
    r = yield from aiohttp.request('get', image_uri)
    raw = yield from r.read()
    image_data = io.BytesIO(raw)
    image_id = yield from bot._client.upload_image(image_data, filename=filename)
    return image_id

@asyncio.coroutine
def image_validate_and_upload_single(text, bot, reject_googleusercontent=True):
    image_id = False
    image_link = image_validate_link(text, reject_googleusercontent=reject_googleusercontent)
    if image_link:
        image_id = yield from image_upload_single(image_link, bot)
    return image_id
