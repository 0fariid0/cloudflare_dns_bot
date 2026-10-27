"""Small shared Telegram rendering and bounded list navigation helpers."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest


def compact_rows(buttons, width=2):
    return [buttons[i:i + width] for i in range(0, len(buttons), width)]


def page_items(items, context, section, size=10):
    count = max(1, (len(items) + size - 1) // size)
    page = max(0, min(int(context.user_data.get('page_' + section, 0)), count - 1))
    context.user_data['page_' + section] = page
    row = []
    if page:
        row.append(InlineKeyboardButton('⬅️ قبلی', callback_data=f'ui_page_{section}_{page-1}'))
    if count > 1:
        row.append(InlineKeyboardButton(f'{page+1}/{count}', callback_data='noop'))
    if page + 1 < count:
        row.append(InlineKeyboardButton('بعدی ➡️', callback_data=f'ui_page_{section}_{page+1}'))
    return items[page*size:(page+1)*size], [row] if row else [], page


async def render(update, text, keyboard):
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        try:
            return await update.effective_message.edit_text(text, reply_markup=markup)
        except BadRequest as exc:
            error=str(exc).lower()
            if 'message is not modified' in error:return
            if any(reason in error for reason in ('message to edit not found', "message can't be edited")):
                return await update.effective_message.reply_text(text,reply_markup=markup)
            raise
    else:
        return await update.effective_message.reply_text(text, reply_markup=markup)
