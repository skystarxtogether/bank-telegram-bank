# coding: utf-8

import os
from uuid import uuid4
import logging
from typing import Mapping, Any
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

import telegram
from telegram import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    ParseMode)

from telegram.ext import (
    Updater,
    RegexHandler,
    CommandHandler,
    InlineQueryHandler
)

from telegram.ext.dispatcher import run_async


from bot_exceptions import BotArgumentParsingError
import plotting
import utils
from settings import logger, DEFAULT_CURRENCY, DEFAULT_PARSER_NAME


API_ENV_NAME = 'BANK_BOT_AP_TOKEN'
CACHE_EXPIRACY_MINUTES = 60
IMAGES_FOLDER = "img"
USER_BANK_SELECTION_CACHE = {}


api_token = os.environ.get(API_ENV_NAME, '')

if not api_token:
    raise ValueError("No API token specified.")


def get_user_selected_bank(user_id: str,
                           cache: Mapping[str, str]=USER_BANK_SELECTION_CACHE) -> str:
    """Finds out whether the given user has bank associated with,
    if not - returns the default one"""
    if user_id not in cache:
        bank_name = DEFAULT_PARSER_NAME
    else:
        bank_name = cache[user_id]
    return bank_name


def set_user_default_bank(user_id: str,
                          bank_name: str,
                          cache: Mapping[str, str]=USER_BANK_SELECTION_CACHE) -> None:
    cache[user_id] = bank_name


def start(bot, update):
    bot.sendMessage(chat_id=update.message.chat_id,
                    text="I'm a bot, please talk to me!")


def unknown(bot, update):
    bot.sendMessage(chat_id=update.message.chat_id,
                    text="Sorry, I didn't understand that command.")


def error(bot, update, error):
    logger.warn('Update "%s" caused error "%s"' % (update, error))


def parse_args(bot, update, args) -> Mapping[str, Any]:
    try:
        preferences = utils.preferences_from_args(args)
    except BotArgumentParsingError as e:
        logger.exception(str(e))
        bot.sendMessage(chat_id=update.message.chat_id,
                        text=str(e))
        return {}

    return preferences


@run_async
def course(bot, update, args, **kwargs):
    chat_id = update.message.chat_id

    preferences = parse_args(bot, update, args)
    if not preferences:
        return

    user_id = str(update.message.from_user.id)
    days_diff = preferences['days_ago']
    bank_name = preferences['bank_name']
    if not bank_name:
        bank_name = get_user_selected_bank(user_id)

    parser = utils.get_parser(bank_name)
    parser_instance = parser()

    parse_date = utils.get_date_from_date_diff(days_diff)
    logger.info("Requesting course for {}".format(str(parse_date)))

    if preferences['currency'] == 'all':
        # We need to send data about all of the currencies
        all_currencies = parser_instance.get_all_currencies(date=parse_date)
        displayed_values = ['{}: {} {}'.format(x.iso, x.sell, x.buy)
                            for x in all_currencies]
        currencies_text_value = "\n".join(displayed_values)
        bot.sendChatAction(chat_id=chat_id, action=telegram.ChatAction.TYPING)
        bot.sendMessage(chat_id=chat_id,
                        text="Currencies: \n{}".format(currencies_text_value))

        return

    currency = preferences['currency']
    if currency.upper() in parser.allowed_currencies:
        # TODO: unify passing currency names (lowercase or uppercase only)
        cur = parser_instance.get_currency(currency_name=currency,
                                           date=parse_date)

        if cur.name == 'NoValue':
            bot.sendMessage(chat_id=chat_id,
                            text="Unknown currency: {}".format(args[0]))
            return
        else:
            text = "{}: {} {}".format(cur.iso, cur.sell, cur.buy)
            bot.sendMessage(chat_id=chat_id,
                            text=text)
            return
    else:
        text = "Unknown currency: {}".format(currency)
        bot.sendMessage(chat_id=chat_id,
                        text=text)
        return


def result_date_saver(parser, currency, date):
    return (date, parser.get_currency(currency, date))


@run_async
def show_currency_graph(bot, update, args, **kwargs):
    """Sends user currency graph changes for the specified period of time.
    E.g. user wants to get exchange rates for the US currency for 10 last days,
    he needs to send something like '/graph USD -d 10' """

    user_id = str(update.message.from_user.id)
    chat_id = update.message.chat_id
    bot.sendChatAction(chat_id=chat_id, action=telegram.ChatAction.TYPING)

    preferences = parse_args(bot, update, args)
    if not preferences:
        return

    days_diff = preferences['days_ago']
    currency = preferences['currency']
    bank_name = preferences['bank_name']
    if not bank_name:
        bank_name = get_user_selected_bank(user_id)

    parser = utils.get_parser(bank_name)
    parser_instance = parser()

    if currency == 'all':
        currency = DEFAULT_CURRENCY.upper()

    date_diffs = utils.date_diffs_for_long_diff(days_diff)

    dates = [utils.get_date_from_date_diff(d) for d in date_diffs]
    past_date, future_date = dates[0], dates[-1]

    plot_image_name = plotting.generate_plot_name(parser.short_name, currency,
                                                  past_date, future_date)

    if not os.path.exists(IMAGES_FOLDER):
        try:
            os.mkdir(IMAGES_FOLDER)
        except OSError as e:
            logger.error("Error creating images folder: ".format(e))
    output_file = os.path.join(IMAGES_FOLDER, plot_image_name)

    if not is_image_cached(output_file):

        # We use thread pool to asyncronously get pages
        currencies_deque = deque()
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_date = {executor.submit(result_date_saver,
                                              parser_instance,
                                              currency, date): date
                              for date in dates}
            for future in as_completed(future_to_date):
                data = future.result()
                currencies_deque.append(data)

        currencies = utils.sort_by_value(currencies_deque, dates)

        logging.info("Creating a plot.")
        x = [d for d in dates]
        y_buy = [c.buy for c in currencies]
        y_sell = [c.sell for c in currencies]
        plotting.render_exchange_rate_plot(x, y_buy, y_sell, output_file)
        plotting.reset_plot()

    bot.sendPhoto(chat_id=chat_id,
                  photo=open(output_file, 'rb'))
    return


def help_user(bot, update):
    chat_id = update.message.chat_id
    help_message = """Use following commands:
/course -d <days ago> -c <currency name>  - display current exchange rate for the
given currency or for all available currencies.
/graph -d <days ago> -c <currency name> - plot currency exchange
rate dynamincs for the specified period of time
/banks - list names of currently supported banks.
/set <bank_name> - sets default bank name for all of the operations
"""
    bot.sendMessage(chat_id=chat_id,
                    text=help_message)

    return


def list_banks(bot, update):
    """Show user names of banks that are supported"""
    chat_id = update.message.chat_id
    parser_classes = utils.get_parser_classes()

    bank_names = "\n".join(
        parser_cls.name + "\t:\t" + parser_cls.short_name
        for parser_cls in parser_classes
    )

    msg = "Current banks are now supported: \n{}".format(bank_names)
    bot.sendMessage(chat_id=chat_id,
                    text=msg)
    return


def set_default_bank(bot, update, args):
    chat_id = update.message.chat_id
    user_id = str(update.message.from_user.id)

    if len(args) != 1:
        msg = "Incorrect number of arguments, please specify bank name"
        bot.sendMessage(chat_id=chat_id,
                        text=msg)
        return
    bank_name = args[0].upper()

    available_names = utils.get_bank_names()
    bank_names_lower = set(map(lambda x: x.lower(), available_names))
    if bank_name.lower() not in bank_names_lower:
        bank_names = ", ".join(available_names)
        msg = "Incorrect bank name specified, available names are: {}"
        bot.sendMessage(chat_id=chat_id,
                        text=msg.format(bank_names))
        return
    set_user_default_bank(user_id, bank_name)
    msg = "Default bank succesfully set to {}"
    bot.sendMessage(chat_id=chat_id,
                    text=msg.format(bank_name))


def inline_rate(bot, update):
    query = update.inline_query.query
    results = list()

    parser_classes = utils.get_parser_classes()
    parsers = [parser()
               for parser in parser_classes
               if parser.short_name != 'mtb']

    for parser in parsers:
        if query.upper() not in parser.allowed_currencies:
            continue
        cur_value = parser.get_currency(query.upper())
        bank_name = parser.name
        text = "{}\n<b>{}</b>: {}".format(bank_name,
                                          query.upper(),
                                          cur_value.sell)
        mes_content = InputTextMessageContent(text,
                                              parse_mode=ParseMode.HTML)

        result = InlineQueryResultArticle(id=uuid4(),
                                          title=parser.name,
                                          input_message_content=mes_content)
        results.append(result)

    bot.answerInlineQuery(update.inline_query.id, results)


def is_image_cached(image_path: str, max_n: int=8) -> bool:
    """Checks whether image with the given name has already been created"""
    return os.path.exists(image_path)


def main():
    updater = Updater(token=api_token)

    dispatcher = updater.dispatcher

    dispatcher.addHandler(CommandHandler('start', start))
    dispatcher.addHandler(CommandHandler('help', help_user))
    dispatcher.addHandler(CommandHandler('course', course, pass_args=True))
    dispatcher.addHandler(CommandHandler('graph', show_currency_graph, pass_args=True))
    dispatcher.addHandler(CommandHandler('banks', list_banks))
    dispatcher.addHandler(CommandHandler('set', set_default_bank, pass_args=True))
    inline_rate_handler = InlineQueryHandler(inline_rate)
    dispatcher.addHandler(inline_rate_handler)

    # log all errors
    dispatcher.addErrorHandler(error)

    unknown_handler = RegexHandler(r'/.*', unknown)
    dispatcher.addHandler(unknown_handler)

    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()
