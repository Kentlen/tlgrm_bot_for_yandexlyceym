import codecs
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import Counter
from io import BytesIO
from urllib.parse import urlparse

import requests
import simplejson
from datetime import datetime
from PIL import Image
from requests.exceptions import InvalidURL, HTTPError, RequestException, ConnectionError, Timeout, ConnectTimeout
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, \
    InputTextMessageContent, InlineQueryResultCachedDocument
from telegram.error import TelegramError, TimedOut, BadRequest, Unauthorized
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler, InlineQueryHandler, \
    ChosenInlineResultHandler, CallbackContext
from telegram.ext.dispatcher import run_async

logging.getLogger("urllib3.connection").setLevel(logging.CRITICAL)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO,
                    filename="ez-sticker-bot.log", filemode="a+")
logger = logging.getLogger(__name__)

directory = os.path.dirname(__file__)

bot: Bot = None

config = {}
users = {}
lang = {}

recent_uses = {}


def main():
    load_files()

    updater = Updater(config['1292603883:AAFCiKSUlAHnVCtPrim_uui2xahlmPjEBs0'], use_context=True, workers=10)
    dispatcher = updater.dispatcher
    global bot
    bot = updater.bot

    dispatcher.add_handler(MessageHandler(~ Filters.private, do_fucking_nothing))

    dispatcher.add_handler(CommandHandler('broadcast', broadcast_command))
    dispatcher.add_handler(CommandHandler('icon', icon_command))
    dispatcher.add_handler(CommandHandler('info', info_command))
    dispatcher.add_handler(CommandHandler('lang', change_lang_command))
    dispatcher.add_handler(CommandHandler('langstats', lang_stats_command))
    dispatcher.add_handler(CommandHandler('log', log_command))
    dispatcher.add_handler(CommandHandler(['optin', 'optout'], opt_command))
    dispatcher.add_handler(CommandHandler('restart', restart_command))
    dispatcher.add_handler(CommandHandler('start', start_command))
    dispatcher.add_handler(CommandHandler('stats', stats_command))

    dispatcher.add_handler(MessageHandler(Filters.command, invalid_command))

    dispatcher.add_handler(MessageHandler((Filters.photo | Filters.document), image_received))
    dispatcher.add_handler(MessageHandler(Filters.sticker, sticker_received))
    dispatcher.add_handler(MessageHandler(Filters.text, url_received))
    dispatcher.add_handler(MessageHandler(Filters.all, invalid_content))

    dispatcher.add_handler(CallbackQueryHandler(change_lang_callback, pattern="lang"))
    dispatcher.add_handler(CallbackQueryHandler(icon_cancel_callback, pattern="icon_cancel"))

    dispatcher.add_handler(InlineQueryHandler(share_query_received, pattern=re.compile("^share$", re.IGNORECASE)))
    dispatcher.add_handler(InlineQueryHandler(file_id_query_received, pattern=re.compile("")))
    dispatcher.add_handler(InlineQueryHandler(share_query_received))

    dispatcher.add_handler(ChosenInlineResultHandler(inline_result_chosen))

    updater.job_queue.run_repeating(save_files, config['save_interval'], config['save_interval'])

    dispatcher.add_error_handler(handle_error)

    updater.start_polling(clean=True, timeout=99999)

    print("Bot finished starting")

    updater.idle()



@run_async
def image_received(update: Update, context: CallbackContext):
    message = update.message
    user_id = message.from_user.id

    cooldown_info = user_on_cooldown(user_id)
    if cooldown_info[0]:
        minutes = int(config['spam_interval'] / 60)
        message_text = get_message(user_id, 'spam_limit_reached').format(config['spam_max'], minutes, cooldown_info[1],
                                                                         cooldown_info[2])
        message.reply_markdown(message_text)
        return

    if message.document:
        document = message.document
        if document.mime_type.lower() in ('image/png', 'image/jpeg', 'image/webp'):
            photo_id = document.file_id
        else:
            bot.send_chat_action(user_id, 'typing')

            message.reply_markdown(get_message(user_id, 'doc_not_img'))
            return
    else:
        photo_id = message.photo[-1].file_id

    bot.send_chat_action(user_id, 'upload_document')

    try:
        download_path = download_file(photo_id)
        image = Image.open(download_path)

        create_sticker_file(message, image, context)

        os.remove(download_path)
    except TimedOut:
        message.reply_text(get_message(user_id, "send_timeout"))
    except FileNotFoundError:
        pass


@run_async
def sticker_received(update: Update, context: CallbackContext):
    message = update.message
    user_id = message.from_user.id

    cooldown_info = user_on_cooldown(user_id)
    if cooldown_info[0]:
        minutes = int(config['spam_interval'] / 60)
        message_text = get_message(user_id, 'spam_limit_reached').format(config['spam_max'], minutes, cooldown_info[1],
                                                                         cooldown_info[2])
        message.reply_markdown(message_text)
        return

    if message.sticker.is_animated:
        animated_sticker_received(update, context)
        return

    sticker_id = message.sticker.file_id

    bot.send_chat_action(user_id, 'upload_document')

    try:
        download_path = download_file(sticker_id)

        image = Image.open(download_path)
        create_sticker_file(message, image, context)

        os.remove(download_path)
    except Unauthorized:
        pass
    except TelegramError:
        message.reply_text(get_message(user_id, "send_timeout"))
    except FileNotFoundError:
        pass


def animated_sticker_received(update: Update, context: CallbackContext):
    message = update.message
    user_id = message.from_user.id

    bot.send_chat_action(user_id, 'upload_document')

    sticker_id = message.sticker.file_id

    try:
        download_path = download_file(sticker_id)

        document = open(download_path, 'rb')
        sticker_message = message.reply_document(document=document)
        sent_message = sticker_message.reply_markdown(get_message(user_id, "forward_animated_sticker"), quote=True)

        file_id = sticker_message.sticker.file_id
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(get_message(user_id, "forward"), switch_inline_query=file_id)]])
        sent_message.edit_reply_markup(reply_markup=markup)

        os.remove(download_path)
    except TelegramError:
        message.reply_text(get_message(user_id, "send_timeout"))
    except FileNotFoundError:
        pass

    record_use(user_id, context)

    global config
    config['uses'] += 1
    global users
    users[str(user_id)]['uses'] += 1



@run_async
def url_received(update: Update, context: CallbackContext):
    message = update.message
    user_id = message.from_user.id
    text = message.text.split(' ')

    cooldown_info = user_on_cooldown(user_id)
    if cooldown_info[0]:
        message.reply_markdown(get_message(user_id, 'spam_limit_reached').format(cooldown_info[1], cooldown_info[2]))
        return

    if len(text) > 1:
        message.reply_text(get_message(message.chat_id, "too_many_urls"))
        return

    text = text[0]
    url = urlparse(text, 'https').geturl()

    if url.lower().startswith("https:///"):
        url = url.replace("https:///", "https://", 1)

    try:
        request = requests.get(url, timeout=3)
        request.raise_for_status()
    except InvalidURL:
        message.reply_markdown(get_message(message.chat_id, "invalid_url").format(url))
        return
    except HTTPError:
        message.reply_markdown(get_message(message.chat_id, "url_does_not_exist").format(url))
        return
    except Timeout or ConnectTimeout:
        message.reply_markdown(get_message(message.chat_id, "url_timeout").format(url))
        return
    except ConnectionError or RequestException or UnicodeError:
        message.reply_markdown(get_message(message.chat_id, "unable_to_connect").format(url))
        return
    except UnicodeError:
        message.reply_markdown(get_message(message.chat_id, "unable_to_connect").format(url))
        return

    try:
        image = Image.open(BytesIO(request.content))
    except OSError:
        message.reply_markdown(get_message(message.chat_id, "url_not_img").format(url))
        return

    bot.send_chat_action(message.chat_id, 'upload_document')

    create_sticker_file(message, image, context)


def create_sticker_file(message, image, context: CallbackContext):
    user_id = message.from_user.id
    user_data = context.user_data

    if 'make_icon' not in user_data:
        user_data['make_icon'] = False

    if user_data['make_icon']:
        image.thumbnail((100, 100), Image.ANTIALIAS)
        background = Image.new('RGBA', (100, 100), (255, 255, 255, 0))
        background.paste(image, (int(((100 - image.size[0]) / 2)), int(((100 - image.size[1]) / 2))))
        image = background

    else:
        width, height = image.size
        reference_length = max(width, height)
        ratio = 512 / reference_length
        new_width = width * ratio
        new_height = height * ratio
        if new_width % 1 >= .999:
            new_width = int(round(new_width))
        else:
            new_width = int(new_width)
        if new_height % 1 >= .999:
            new_height = int(round(new_height))
        else:
            new_height = int(new_height)

        image = image.resize((new_width, new_height), Image.ANTIALIAS)

    temp_path = os.path.join(temp_dir(), (uuid.uuid4().hex[:6].upper() + '.png'))
    image.save(temp_path, format="PNG", optimize=True)

    document = open(temp_path, 'rb')
    try:
        filename = 'icon.png' if user_data['make_icon'] else 'sticker.png'
        sent_message = message.reply_document(document=document, filename=filename,
                                              caption=get_message(user_id, "forward_to_stickers"), quote=True,
                                              timeout=30)
        file_id = sent_message.document.file_id
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(get_message(user_id, "forward"), switch_inline_query=file_id)]])
        sent_message.edit_reply_markup(reply_markup=markup)
    except Unauthorized:
        pass
    except TelegramError:
        message.reply_text(get_message(user_id, "send_timeout"))

    image.close()
    time.sleep(0.2)
    os.remove(temp_path)

    if user_data['make_icon']:
        user_data['make_icon'] = False

    record_use(user_id, context)

    global config
    config['uses'] += 1
    global users
    users[str(user_id)]['uses'] += 1


def download_file(file_id):
    try:
        file = bot.get_file(file_id=file_id, timeout=30)
        ext = '.' + file.file_path.split('/')[-1].split('.')[1]
        download_path = os.path.join(temp_dir(), (file_id + ext))
        file.download(custom_path=download_path)

        return download_path
    except TimedOut:
        raise TimedOut


@run_async
def change_lang_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    lang_code = query.data.split(':')[-1]
    user_id = str(query.from_user.id)

    global users
    users[user_id]['lang'] = lang_code

    message = get_message(user_id, "lang_set").split(' ')
    for i in range(len(message)):
        word = message[i]
        if word[0] == '$':
            try:
                _id = int(''.join(c for c in word if c.isdigit()))
                user = bot.get_chat(_id)
                message[i] = '<a href="tg://user?id={}">{}{}</a>'.format(_id, user.first_name,
                                                                         ' ' + user.last_name if user.last_name else '')
            except ValueError:
                message[i] = 'UNKNOWN_USER_ID'
                continue
            except TelegramError:
                message[i] = 'INVALID_USER_ID'
                continue
    message = ' '.join(message)

    users[user_id]['icon_warned'] = False

    query.edit_message_text(text=message, reply_markup=None, parse_mode='HTML')
    query.answer()


@run_async
def share_query_received(update: Update, context: CallbackContext):
    query = update.inline_query
    user_id = query.from_user.id

    title = get_message(user_id, "share")
    description = get_message(user_id, "share_desc")
    thumb_url = config['share_thumb_url']
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text=get_message(user_id, "make_sticker_button"), url="https://t.me/StickerBot")]])
    input_message_content = InputTextMessageContent(get_message(user_id, "share_text"), parse_mode='Markdown')

    results = [InlineQueryResultArticle(id="share", title=title, description=description, thumb_url=thumb_url,
                                        reply_markup=markup, input_message_content=input_message_content)]
    try:
        query.answer(results=results, cache_time=5, is_personal=True)
    except BadRequest as e:
        if e.message == "Query is too old and response timeout expired or query id is invalid":
            return
        else:
            raise e


@run_async
def file_id_query_received(update: Update, context: CallbackContext):
    query = update.inline_query
    user_id = query.from_user.id
    results = None

    try:
        file = bot.get_file(query.query)

        _id = uuid.uuid4()
        title = get_message(user_id, "your_sticker")
        desc = get_message(user_id, "forward_desc")
        caption = "@StickerBot"
        results = [InlineQueryResultCachedDocument(_id, title, file.file_id, description=desc, caption=caption)]

        query.answer(results=results, cache_time=5, is_personal=True)
    except TelegramError:
        share_query_received(update, context)


@run_async
def icon_cancel_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = str(query.from_user.id)

    context.user_data['make_icon'] = False

    query.edit_message_text(text=get_message(user_id, "icon_canceled"), reply_markup=None)
    query.answer()


@run_async
def inline_result_chosen(update: Update, context: CallbackContext):
    chosen_result = update.chosen_inline_result
    result_id = chosen_result.result_id

    global config

    if result_id == 'share':
        config['times_shared'] += 1


@run_async
def invalid_command(update: Update, context: CallbackContext):
    message = update.message

    bot.send_chat_action(message.chat_id, 'typing')
    message.reply_text(get_message(message.chat_id, "invalid_command"))


@run_async
def invalid_content(update: Update, context: CallbackContext):
    message = update.message

    bot.send_chat_action(message.chat_id, 'typing')

    message.reply_text(get_message(message.chat_id, "cant_process"))
    message.reply_markdown(get_message(message.chat_id, "send_sticker_photo"))


def do_fucking_nothing(update: Update, context: CallbackContext):
    pass



@run_async
def broadcast_command(update: Update, context: CallbackContext):
    message = update.message
    chat_id = message.chat_id

    bot.send_chat_action(chat_id, 'typing')

    if chat_id not in config['admins']:
        message.reply_text(get_message(chat_id, "no_permission"))
        return

    target_message = message.reply_to_message

    if target_message is None:
        message.reply_markdown(get_message(chat_id, "broadcast_in_reply"))
        return

    broadcast_message = target_message.text_html
    if broadcast_message is None:
        message.reply_markdown(get_message(chat_id, "broadcast_only_text"))
        return

    message.reply_text(get_message(chat_id, "will_broadcast"))
    context.job_queue.run_once(broadcast_thread, 2, context=broadcast_message)


@run_async
def change_lang_command(update: Update, context: CallbackContext):
    message = update.message
    ordered_langs = [None] * len(lang)
    for lang_code in lang.keys():
        ordered_langs[int(lang[lang_code]['order'])] = lang_code
    keyboard = [[]]
    row = 0
    for lang_code in ordered_langs:
        if len(keyboard[row]) == 3:
            row += 1
            keyboard.append([])
        keyboard[row].append(
            InlineKeyboardButton(lang[lang_code]['lang_name'], callback_data="lang:{}".format(lang_code)))
    markup = InlineKeyboardMarkup(keyboard)
    message.reply_text(get_message(message.chat_id, "select_lang"), reply_markup=markup)


@run_async
def icon_command(update: Update, context: CallbackContext):
    message = update.message

    bot.send_chat_action(message.chat_id, 'typing')

    context.user_data['make_icon'] = True

    keyboard = [[
        InlineKeyboardButton(get_message(message.chat_id, "cancel"), callback_data="icon_cancel")
    ]]
    markup = InlineKeyboardMarkup(keyboard)

    if not get_user_config(message.chat_id, 'icon_warned'):
        message.reply_markdown(get_message(message.chat_id, "icon_command_info"))

        global users
        users[str(message.chat_id)]['icon_warned'] = True

    message.reply_markdown(get_message(message.chat_id, "icon_command"), reply_markup=markup)


@run_async
def info_command(update: Update, context: CallbackContext):
    message = update.message

    bot.send_chat_action(message.chat_id, 'typing')
    keyboard = [
        [InlineKeyboardButton(get_message(message.chat_id, "contact_dev"), url=config['contact_dev_link']),
         InlineKeyboardButton(get_message(message.chat_id, "source"),
                              url=config['source_link'])],
        [InlineKeyboardButton(get_message(message.chat_id, "rate"),
                              url=config['rate_link']),
         InlineKeyboardButton(get_message(message.chat_id, "share"), switch_inline_query="share")]]
    markup = InlineKeyboardMarkup(keyboard)
    message.reply_markdown(get_message(message.chat_id, "info").format(config['uses']), reply_markup=markup)


@run_async
def lang_stats_command(update: Update, context: CallbackContext):
    message = update.message

    bot.send_chat_action(message.chat_id, 'typing')

    lang_stats_message = get_message(message.chat_id, "lang_stats")

    langs = [user['lang'] for user in users.values()]
    lang_usage = dict(Counter(langs))

    sorted_usage = [(code, lang_usage[code]) for code in sorted(lang_usage, key=lang_usage.get, reverse=True)]

    message_lines = {}
    for code, count in sorted_usage:
        lang_stats_message += "\n" + u"\u200E" + "{}: {:,}".format(lang[code]['lang_name'], count)

    for index in range(0, len(lang)):
        try:
            lang_stats_message += message_lines[str(index)]
        except KeyError:
            continue

    message.reply_markdown(lang_stats_message)


@run_async
def log_command(update: Update, context: CallbackContext):
    message = update.message

    if message.from_user.id in config['admins']:
        bot.send_chat_action(message.chat_id, 'upload_document')

        log_file_path = os.path.join(directory, 'ez-sticker-bot.log')
        with open(log_file_path, 'rb') as log_document:
            try:
                message.reply_document(log_document)
            except BadRequest:
                message.reply_text(get_message(message.chat_id, "empty_log"))
            log_document.close()

    else:
        bot.send_chat_action(message.chat_id, 'typing')

        message.reply_text(get_message(message.chat_id, "no_permission"))


@run_async
def opt_command(update: Update, context: CallbackContext):
    message = update.message

    bot.send_chat_action(message.chat_id, 'typing')

    global users
    user_id = str(message.from_user.id)
    opt_in = get_user_config(user_id, "opt_in")

    command = message.text.split(' ')[0][1:].lower()
    if command == 'optin':
        if opt_in:
            message.reply_text(get_message(user_id, "already_opted_in"))
        else:
            users[user_id]['opt_in'] = True
            message.reply_text(get_message(user_id, "opted_in"))
    else:
        if not opt_in:
            message.reply_text(get_message(user_id, "already_opted_out"))
        else:
            users[user_id]['opt_in'] = False
            message.reply_text(get_message(user_id, "opted_out"))


def restart_command(update: Update, context: CallbackContext):
    message = update.message

    bot.send_chat_action(message.chat_id, 'typing')
    if message.from_user.id in config['admins']:
        message.reply_text(get_message(message.chat_id, "restarting"))
        save_files()
        logger.info("Bot restarted by {} ({})".format(message.from_user.first_name, message.from_user.id))
        os.execl(sys.executable, sys.executable, *sys.argv)
    else:
        message.reply_text(get_message(message.chat_id, "no_permission"))


@run_async
def start_command(update: Update, context: CallbackContext):
    message = update.message

    bot.send_chat_action(message.chat_id, 'typing')
    message.reply_markdown(get_message(message.chat_id, "start"))


@run_async
def stats_command(update: Update, context: CallbackContext):
    message = update.message
    user_id = message.chat_id

    bot.send_chat_action(user_id, 'typing')

    opted_in = 0
    opted_out = 0
    for user in users.values():
        if user['opt_in']:
            opted_in += 1
        else:
            opted_out += 1

    personal_uses = get_user_config(user_id, "uses")
    stats_message = get_message(user_id, "stats").format(config['uses'], len(users), personal_uses,
                                                         config['langs_auto_set'], config['times_shared'],
                                                         opted_in + opted_out, opted_in, opted_out)
    message.reply_markdown(stats_message)



def record_use(user_id, context: CallbackContext):
    user_id = str(user_id)

    global recent_uses
    if user_id not in recent_uses:
        recent_uses[user_id] = []

    job = context.job_queue.run_once(remove_use, config['spam_interval'], context=(user_id, datetime.now()))
    recent_uses[user_id].append(job)


def remove_use(context: CallbackContext):
    job = context.job
    user_id = job.context[0]
    global recent_uses
    recent_uses[user_id].remove(job)


def user_on_cooldown(user_id):
    user_id = str(user_id)

    recent_uses_count = len(recent_uses[user_id]) if user_id in recent_uses else 0
    on_cooldown = recent_uses_count >= config['spam_max']

    if on_cooldown:
        oldest_job_time = recent_uses[user_id][0].context[1]
        seconds_left = int(config['spam_interval'] - (datetime.now() - oldest_job_time).total_seconds())
        time_left = divmod(seconds_left, 60)
    else:
        time_left = 0, 0

    if time_left[0] == 0 and time_left[1] == 0:
        on_cooldown = False

    return on_cooldown, time_left[0], time_left[1]


@run_async
def broadcast_thread(context: CallbackContext):
    if context.job.context is None:
        print("Broadcast thread created without message stored in job context")
        return

    global config
    index = 0
    for user_id in list(users):
        opt_in = get_user_config(user_id, "opt_in")

        try:
            if opt_in and not config['override_opt_out']:
                bot.send_message(chat_id=int(user_id), text=context.job.context, parse_mode='HTML',
                                 disable_web_page_preview=True)
                if config['send_opt_out_message']:
                    bot.send_message(chat_id=int(user_id), text=get_message(user_id, "opt_out_info"))
        except Unauthorized:
            pass
        except TelegramError as e:
            logger.warning("Error '{}' when broadcasting message to {}".format(e.message, user_id))

        index += 1
        if index >= config['broadcast_batch_size']:
            time.sleep(config['broadcast_batch_interval'])
            index = 0


def get_message(user_id, message):
    lang_pref = get_user_config(user_id, "lang")

    if message not in lang[lang_pref]:
        lang_pref = 'en'

    return lang[lang_pref][message]


def get_user_config(user_id, key):
    global users
    user_id = str(user_id)

    if user_id not in users:
        users[user_id] = config['default_user'].copy()

        lang_code = bot.get_chat(user_id).get_member(user_id).user.language_code.lower()
        if lang_code is not None and lang_code[:2] in lang:
            users[user_id]['lang'] = lang_code[:2]
            if lang_code[:2] != 'en':
                config['langs_auto_set'] += 1
    elif key not in users[user_id]:
        try:
            users[user_id][key] = config['default_user'][key].copy()
        except AttributeError:
            users[user_id][key] = config['default_user'][key]

    return users[user_id][key]


def handle_error(update: Update, context: CallbackContext):
    if context.error in ():
        return
    logger.warning('Update'.format(update, context.error))


def load_lang():
    path = os.path.join(directory, 'lang.json')
    data = json.load(codecs.open(path, 'r', 'utf-8-sig'))
    for lang_code in data:
        for message in data[lang_code]:
            data[lang_code][message] = data[lang_code][message].replace('\\n', '\n')
    return data


def load_json(file_name):
    path = os.path.join(directory, file_name if '.' in file_name else file_name + '.json')
    with open(path) as json_file:
        data = json.load(json_file)
    json_file.close()
    return data


def save_json(json_obj, file_name):
    data = json.dumps(json_obj)
    path = os.path.join(directory, file_name if '.' in file_name else file_name + '.json')
    with open(path, "w") as json_file:
        json_file.write(simplejson.dumps(simplejson.loads(data), indent=4, sort_keys=True))
    json_file.close()


def load_files():
    try:
        global config
        config = load_json('config.json')
    except FileNotFoundError:
        sys.exit("config.json is missing; exiting")
    try:
        global lang
        lang = load_lang()
    except FileNotFoundError:
        sys.exit("lang.json is missing; exiting")
    try:
        global users
        users = load_json('users.json')
    except FileNotFoundError:
        save_json({}, 'users.json')


def save_files(context: CallbackContext = None):
    save_json(config, 'config.json')
    save_json(users, 'users.json')


def temp_dir():
    temp_path = os.path.join(directory, 'temp')
    if not os.path.exists(temp_path):
        os.mkdir(temp_path)
    return temp_path


if __name__ == '__main__':
    main()