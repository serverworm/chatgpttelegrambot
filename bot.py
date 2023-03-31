import logging
import time
import openai
from aiogram import Bot, Dispatcher, executor, types

bot_token = '6232666539:AAGIoB950zeQQ_t-LMc6CSYS14LgZnBmA60'
api_key = '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'

logging.basicConfig(level=logging.INFO)

bot = Bot(token=bot_token)
dp = Dispatcher(bot)

openai.api_key = api_key

messages = {}


@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    try:
        username = message.from_user.username
        messages[username] = []
        await message.answer("* * *Что тебя интересует? 😃* * *", parse_mode='Markdown')
        await message.delete()
    except Exception as e:
        logging.error(f'⚒ Error in start_cmd: {e}\n\nRestart Bot! ')


@dp.message_handler(commands=['newtopic'])
async def new_topic_cmd(message: types.Message):
    try:
        username = message.from_user.username
        messages[username] = []
        await message.reply('* * * \n\n🚧 Начинаем новую тему для выхода из рекурсии!\n\n👀 Чуточку терпения, вот-вот отвечу! * * *', parse_mode='Markdown')
    except Exception as e:
        logging.error(f'⚒ Error in new_topic_cmd: {e}\n\nRestart Bot! ')


@dp.message_handler()
async def echo_msg(message: types.Message):
    try:
        user_message = message.text
        username = message.from_user.username

        if username not in messages:
            messages[username] = []
        messages[username].append({"role": "user", "content": user_message})
        messages[username].append({"role": "user", "content": f"chat: {message.chat} Сейчас {time.strftime('%d/%m/%Y %H:%M:%S')} user: {message.from_user.first_name} message: {message.text}"})
        logging.info(f'{username}: {user_message}')

        should_respond = not message.reply_to_message or message.reply_to_message.from_user.id == bot.id

        if should_respond:
            processing_message = await message.reply(
                '⏱* * * Формирую ответ, пожалуйста подождите * * *',
                parse_mode='Markdown')
            await bot.send_chat_action(chat_id=message.chat.id, action="typing")
            report = []
            for resp in openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=messages[username],
                max_tokens=2500,
                temperature=0.7,
                frequency_penalty=0,
                presence_penalty=0,
                user=username,
                stream=True
            ):
                #print(resp)
                content_value = resp.get("choices")[0].get("delta").get("content")
                if content_value is not None:
                    report.append(resp["choices"][0]["delta"]["content"])
                else: continue

                try:
                    await bot.edit_message_text(
                        text="".join(report),
                        chat_id=message.chat.id,
                        message_id=message.message_id + 1
                    )
                except: continue
            await bot.edit_message_text(
                    text="".join(report),
                    chat_id=message.chat.id,
                    message_id=message.message_id + 1,
                    parse_mode='Markdown')

                # await bot.delete_message(chat_id=processing_message.chat.id, message_id=processing_message.message_id)
            logging.info(f'Response: {"".join(report)}')

    except Exception as ex:
        await message.reply(
                f'* * * \n\n⚠ У бота присутсвует ограничение символов для защиты от сетевых атак, пересоздаю диалог... * * *\n\n🤓 Расшифровка ошибки:\n{ex}',
                parse_mode='Markdown')
        await new_topic_cmd(message)
        await echo_msg(message)


if __name__ == '__main__':
    executor.start_polling(dp)
