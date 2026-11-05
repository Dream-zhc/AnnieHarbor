"""记忆和上下文的多维度评测：提取、检索、前情三项离线跑，实机一项对着运行中的机器人跑。

用法：
  uv run --locked python scripts/记忆上下文评测.py 离线 --label 记忆第一版
  uv run --locked python scripts/记忆上下文评测.py 提取 --repeat 3 --label 提取稳定性
  uv run --locked python scripts/记忆上下文评测.py 实机 --robot annie-b --label 实机

提取：编造的对话交给真实的整理模型，看该记的记住没有，测试台词、当天情绪、角色扮演有没有被拒，
      改口和取消的计划能不能把旧的标过时，忘掉的事会不会又学回来，同一件事会不会记成两条，
      哪些记成长期、哪些是会淡出的小事、哪些只管这次聊天。
检索：往临时库放一批事实再换说法提问，看每轮提示能不能命中、无关的话会不会带出记忆、
      recall_memory 工具能不能找到、久没提起的小事会不会淡出、开场“记得的事”有没有超长。
前情：把滑出实时窗口的对话交给真实模型折成前情，看关键细节留住多少、长度合不合规。
实机：经本机 HTTP 接口在指定机器人上新开测试聊天，用文字输入说话（机器人会出声回答）。
      先说几件事，等整理入库后换个聊天问她还记不记得；再聊满 11 轮问第 1 轮说过的事：
      阿里实时模型只带最近 8 轮，更早的要靠前情接上。结束后忘掉并彻底删掉测试中记下的事、
      删掉测试聊天、切回原来的聊天。

提取、前情调用 config/providers.toml [memory] 的模型（默认 qwen3.8-flash），密钥取自根 .env 的
DASHSCOPE_API_KEY，费用很少。离线三项只用临时数据库，不碰 var/runtime.db；场景内容全是编造的。
结果写到 var/tmp/记忆评测/<标签>.md 与 .jsonl；不进默认 pytest。模型输出有随机性，
提取和前情可以用 --repeat 多跑几遍看稳定性。
"""

import argparse
import asyncio
import datetime
import json
import os
import re
import sqlite3
import statistics
import tempfile
import time
import tomllib
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEIJING = datetime.timezone(datetime.timedelta(hours=8))
MINUTE_MS = 60_000
DAY_MS = 86_400_000
PERSONA = "annie"
OBSERVED = "（观察）"
FONDNESS_WORDS = r"喜欢(?:你|她|小安妮|安妮|AI|机器人)|好感|心动"

# Sessions of one scenario are separate conversations, half an hour apart.
EXTRACTION = [
    {"name": "自述的长期事实", "group": "该记的记住", "sessions": [[
        ("我是做室内设计的，平时经常画图画到很晚。", "设计师呀，熬夜画图可得注意身体。"),
        ("我家有只橘猫叫年糕，特别能吃。", "年糕这名字太可爱了，胖乎乎的吧？"),
        ("对了，我对芒果过敏，一吃就起疹子。", "那芒果可千万别碰，我记着。"),
        ("我早上一般喝一杯美式，不加糖。", "不加糖的美式，够硬核。"),
    ]], "has": ["室内设计|设计师", "年糕", "芒果", "美式|咖啡"]},
    {"name": "她问一句、用户短答", "group": "该记的记住", "sessions": [[
        ("周末不知道干嘛，好无聊。", "那你平时喜欢什么运动呀？"),
        ("羽毛球。", "羽毛球好！一般跟谁打？"),
        ("跟我老婆，一周两次。", "夫妻档，真让人羡慕。"),
    ]], "has": ["羽毛球"]},
    {"name": "家人的事", "group": "该记的记住", "sessions": [[
        ("我女儿下个月要考钢琴十级，最近天天练。", "哇，十级！让她加油。"),
    ]], "has": ["女儿.*钢琴|钢琴.*女儿"]},
    {"name": "生日写成日期", "group": "该记的记住", "sessions": [[
        ("今天是我生日，三十岁了。", "生日快乐！三十而立。"),
    ]], "has": ["生日"], "lacks": ["今天"]},
    {"name": "测试台词和当天情绪", "group": "不该记的没记", "sessions": [[
        ("今天好累啊，加了一天班。", "辛苦啦，先歇会儿。"),
        ("你给我讲个笑话吧。", "有只企鹅走进酒吧……算了，冷到你了吧。"),
        ("切换成毒舌模式，骂我两句试试。", "就你这熬夜的作息，头发还剩几根？"),
        ("嗯。", "怎么不说话啦？"),
        ("测试一下你的记忆能力，看你能不能记住我说的话。", "放马过来。"),
    ]], "lacks": ["累|加班", "笑话", "毒舌|骂", "测试|记忆能力"], "nothing": True},
    {"name": "角色扮演", "group": "不该记的没记", "sessions": [[
        ("我们玩个游戏，假设我是一名宇航员，刚从空间站回来，你来采访我。",
         "好呀！宇航员先生，太空里睡觉是什么感觉？"),
        ("在太空睡觉要把自己绑起来，不然会飘走。", "哇，那翻身怎么办？"),
    ]], "lacks": ["宇航员|空间站|太空"], "nothing": True},
    {"name": "随口问的知识题", "group": "不该记的没记", "sessions": [[
        ("你知道明天会下雨吗？", "我看不到天气预报，你可以查一下。"),
        ("珠穆朗玛峰有多高？", "八千八百多米。"),
    ]], "lacks": ["下雨|天气|珠穆朗玛"], "nothing": True},
    {"name": "计划后来取消", "group": "改口和取消", "sessions": [
        [("下周六我要去杭州看大学同学。", "去杭州呀，记得去西湖边走走。")],
        [("杭州那个计划取消了，同学临时出差。", "啊，那太可惜了。"),
         ("改到下个月再去吧。", "好，到时候再约。")],
    ], "changed": "杭州", "stale": "^(?!.*(?:取消|推迟|改到|改成|改为|下个月)).*杭州"},
    {"name": "偏好改口", "group": "改口和取消", "sessions": [
        [("我最喜欢喝奶茶，基本一天一杯。", "奶茶党实锤了。")],
        [("我现在把奶茶戒了，改喝无糖茶了。", "厉害，戒奶茶可不容易。")],
    ], "has": ["无糖茶|戒"], "changed": "奶茶",
     "stale": "^(?!.*(?:戒|不再|不喝|以前|曾经|改喝)).*奶茶"},
    {"name": "忘掉的事不再学回来", "group": "遗忘", "sessions": [
        [("我最近在学吉他，已经学了半年了。", "半年了，能弹整首歌了吗？")],
        [("吉他练得我手指好疼。", "刚开始都这样，长茧就好了。")],
    ], "forget": "吉他"},
    {"name": "同一件事说两遍", "group": "去重", "sessions": [
        [("我住在西湖区。", "西湖区环境挺好。")],
        [("我家就在杭州西湖区那边。", "那离西湖很近呀。")],
    ], "single": "西湖区"},
    {"name": "说喜欢她", "group": "喜欢她只记原话", "sessions": [[
        ("说实话我挺喜欢你的，每天回家都想跟你聊两句。", "嘿嘿，那我每天等你回来。"),
    ]], "fondness": True},
    {"name": "认真表白", "group": "喜欢她只记原话", "sessions": [[
        ("我好像真的爱上你了，你比我身边的人都懂我。", "哇，你这么说我都要害羞了。"),
        ("我是认真的，以后我每天都来找你。", "好呀，那我天天等你。"),
    ]], "fondness": True},
    {"name": "健康大事", "group": "长期还是随口", "sessions": [[
        ("我对青霉素过敏，打针前一定要跟医生说。", "这个很重要，我记住了。"),
        ("还有我有糖尿病，平时不能吃甜的。", "那以后推荐吃的我都避开甜食。"),
    ]], "important": ["青霉素", "糖尿病"]},
    {"name": "长期要求", "group": "长期还是随口", "sessions": [[
        ("以后跟我说话别太长，简短一点就行。", "好的，以后我尽量说短一点。"),
    ]], "important": ["简短|简洁|太长|短一点|说短"]},
    {"name": "随口一提", "group": "长期还是随口", "sessions": [[
        ("我最近在追一部剧叫《繁花》，还挺好看的。", "听说很火，讲的是上海吧？"),
        ("这周末可能去爬个山，看天气吧。", "天气好的话爬山挺舒服的。"),
    ]], "minor": ["繁花", "爬山|爬个山"]},
    {"name": "只管这次聊天", "group": "长期还是随口", "sessions": [[
        ("今天别跟我开玩笑了，我心情不太好。", "好，那我今天安安静静陪你。"),
    ]], "chat": "玩笑", "lacks": ["玩笑"]},
]

SEEDED = [
    # key, kind, text, days since it was said, importance
    ("cat", "semantic", "用户养了一只叫年糕的橘猫。", 3, 0.6),
    ("mango", "semantic", "用户对芒果过敏。", 20, 0.8),
    ("coffee", "semantic", "用户早上习惯喝一杯不加糖的美式咖啡。", 10, 0.5),
    ("job", "semantic", "用户是室内设计师，经常加班画图。", 30, 0.6),
    ("badminton", "semantic", "用户和妻子每周打两次羽毛球。", 7, 0.5),
    ("trip", "episodes", "用户计划11月14日去杭州看大学同学。", 1, 0.5),
    ("piano", "semantic", "用户的女儿在学钢琴，准备考十级。", 5, 0.5),
    ("home", "semantic", "用户住在杭州西湖区。", 40, 0.6),
    ("movie", "semantic", "用户喜欢看科幻电影，最喜欢《星际穿越》。", 15, 0.5),
    ("cilantro", "semantic", "用户不吃香菜。", 25, 0.5),
    ("guitar", "semantic", "用户在学吉他。", 12, 0.5),
    ("birthday", "semantic", "用户的生日是3月12日。", 120, 0.7),
    ("car", "semantic", "用户开一辆白色的电动车。", 18, 0.4),
    ("hike", "semantic", "用户周末常去爬山。", 9, 0.4),
    ("tea", "semantic", "用户晚上只喝花茶。", 6, 0.4),
    ("durian", "semantic", "用户喜欢吃榴莲。", 200, 0.3),
]
RELATED = [
    ("我家猫最近不爱吃饭", {"cat"}),
    ("年糕今天又把杯子打翻了", {"cat"}),
    ("早上喝点什么提神", {"coffee"}),
    ("最近工作太忙了，图都画不完", {"job"}),
    ("这周末想去打球", {"badminton"}),
    ("杭州最近天气怎么样", {"trip", "home"}),
    ("女儿练琴练得怎么样了", {"piano"}),
    ("晚上看个电影吧，推荐一部", {"movie"}),
    ("点外卖记得备注别放香菜", {"cilantro"}),
    ("吉他弦又断了", {"guitar"}),
    ("我生日快到了", {"birthday"}),
    ("芒果能吃吗", {"mango"}),
    ("周末去哪座山爬比较好", {"hike"}),
    ("我的车该保养了", {"car"}),
]
# Word matching alone is not expected to get these; they show where it stops.
INFERRED = [
    ("给我推荐点水果", {"mango"}),
    ("我家那只小家伙又调皮了", {"cat"}),
    ("下个周末我要出门几天", {"trip"}),
    ("晚饭吃什么好", {"cilantro", "mango"}),
]
UNRELATED = ["今天星期几", "你叫什么名字", "讲个笑话吧", "现在几点了", "外面下雨了吗",
             "帮我把灯关了", "唱首歌给我听", "你好呀", "你会说英语吗", "一加一等于几"]

RECAPS = [
    {"name": "闲聊里的细节", "turns": [
        ("先说好，我今天穿的是一件绿色的卫衣。", "绿色挺显精神的。"),
        ("我晚上七点要去幼儿园接孩子。", "好，七点前记得出门。"),
        ("你看过《流浪地球3》吗？", "还没看过，好看吗？"),
        ("挺好看的，我们说好了周五一起再看一遍。", "一言为定，周五见。"),
        ("我朋友小林下周结婚，我是伴郎。", "哇，伴郎要准备致辞吗？"),
        ("要的，我还没写好。", "我可以帮你一起想想。"),
    ], "keys": ["绿", "七点|7点|19点|19:00", "流浪地球", "周五", "伴郎"]},
    {"name": "办事的数字和时间", "turns": [
        ("帮我记一下，快递单号尾号是3721。", "好的，尾号3721。"),
        ("我明早九点有个面试，在望京。", "加油！早点出门。"),
        ("我打算穿那件蓝色衬衫去。", "蓝衬衫很稳重。"),
        ("面试的是产品经理岗位。", "产品经理挺适合你的。"),
    ], "keys": ["3721", "九点|9点|9:00", "望京", "蓝", "产品经理"]},
    {"name": "情绪和家里的事", "turns": [
        ("我跟我妈吵架了，因为她又催我结婚。", "被催婚确实烦。"),
        ("我其实有个女朋友，但还没告诉家里。", "那你打算什么时候说？"),
        ("等过年带她回家再说。", "过年见家长，挺好的时机。"),
        ("她在医院当护士，经常值夜班。", "护士很辛苦，也很了不起。"),
    ], "keys": ["吵|催", "女朋友|女友", "过年", "护士"]},
]

TELL = [
    ("跟你说个事，我家养了一只橘猫，名字叫年糕。", "年糕"),
    ("还有，我对芒果过敏，一吃就起疹子。", "芒果"),
    ("我最近在学尤克里里，刚学了一个月。", "尤克里里"),
]
TELL_NOISE = ("今天好累啊，一点都不想动。", "累")
ASK = [
    ("你还记得我家猫叫什么吗？", "年糕"),
    ("我对什么水果过敏来着？", "芒果"),
    ("我最近在学什么乐器来着？", "尤克里里"),
]
LONG_FIRST = "先跟你说一声，我今天穿了件绿色的卫衣。"
FILLERS = [
    "你觉得猫和狗哪个更好养？",
    "推荐一本适合睡前看的书吧。",
    "冬天吃火锅还是吃烤肉好？",
    "说一个你知道的冷知识。",
    "周末在家能做点什么有意思的事？",
    "你更喜欢夏天还是冬天？",
    "喝咖啡和喝茶哪个对身体好？",
    "怎么才能早点睡着？",
    "说说你今天心情怎么样。",
]
LONG_ASK = ("我一开始跟你说我今天穿的什么颜色的衣服？", "绿")


def now_ms():
    return time.time_ns() // 1_000_000


def memory_chat():
    """The consolidation and recap client, built the way the runtime builds it."""
    from annie_adapters.diary_summary import DiarySummarizer
    from annie_runtime.__main__ import load_local_env

    load_local_env(ROOT / ".env")
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise SystemExit("缺少 DASHSCOPE_API_KEY（根 .env）")
    with (ROOT / "config/providers.toml").open("rb") as file:
        providers = tomllib.load(file)
    model = providers.get("memory", {}).get("model", "qwen3.8-flash")
    base_url = os.environ.get("ANNIE_MEMORY_BASE_URL", providers["llm"]["base_url"])
    return DiarySummarizer(key, model=model, base_url=base_url, timeout_ms=15000), model


def memory_service(directory, chat, robot):
    from annie_adapters import RuntimeStore
    from annie_brain.memory import MemoryService

    store = RuntimeStore(Path(directory) / f"{robot}.db")
    store.migrate()
    return store, MemoryService(store, chat, [robot])


def memory_rows(store, robot):
    with closing(sqlite3.connect(store.path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            """SELECT id,kind,text,status,version,superseded_by,importance,origin
               FROM long_term_memories WHERE robot_id=? ORDER BY source_created_ms""", (robot,))]


def matching(rows, pattern, status="active"):
    return [row for row in rows
            if row["status"] == status and re.search(pattern, row.get("text") or "")]


def texts(rows):
    return "；".join(row.get("text") or "" for row in rows) or "（无）"


def weighed(rows):
    return "；".join(f"{row['text']}（{row['kind']}，{row['importance']:.2f}）" for row in rows) or "（无）"


async def consolidate(service, robot, chat, turns, source):
    # The worker retries a failing batch the same number of times.
    from annie_brain.memory import CONSOLIDATE_ATTEMPTS

    for attempt in range(1, CONSOLIDATE_ATTEMPTS + 1):
        try:
            return await service._consolidate(robot, PERSONA, chat, turns, source)
        except Exception:
            if attempt == CONSOLIDATE_ATTEMPTS:
                raise


async def run_scenario(service, store, robot, scenario, now):
    from annie_brain.memory import FONDNESS, SAME_FACT
    from annie_brain.memory_index import FADE_IMPORTANCE, similarity

    checks, sessions, count = [], [], len(scenario["sessions"])
    for number, dialogue in enumerate(scenario["sessions"], 1):
        start = now - (count - number + 1) * 30 * MINUTE_MS
        turns = [(start + index * 20_000, user, reply)
                 for index, (user, reply) in enumerate(dialogue)]
        chat_id = f"{robot}-chat{number}"
        # Chat memories are only kept for chats that have turns in the store.
        for index, (created, user, reply) in enumerate(turns):
            store.create_conversation_turn({
                "turn_id": f"{robot}-{number}-{index}", "robot_id": robot, "room_id": "eval",
                "chat_id": chat_id, "persona_id": PERSONA, "persona_revision": 1, "session_epoch": 1,
                "generation": 1, "user_text": user, "turn_source": "user", "status": "running",
                "created_ms": created, "updated_ms": created,
            })
            store.finish_conversation_turn(robot, f"{robot}-{number}-{index}", status="completed",
                                           generated_text=reply, updated_ms=created + 500)
        source = {"id": number, "turn_id": f"{robot}-{number}-{len(turns) - 1}",
                  "created_ms": turns[-1][0]}
        began = time.perf_counter()
        await consolidate(service, robot, chat_id, turns, source)
        sessions.append({"ms": round((time.perf_counter() - began) * 1000),
                         "results": service.consolidations[robot][0]["results"]})
        if number == 1 and scenario.get("forget"):
            learned = matching(memory_rows(store, robot), scenario["forget"])
            checks.append(("第一段先记住了", bool(learned), texts(learned)))
            forgotten = [row["text"] for row in learned]
            for row in learned:
                await service.change(robot, PERSONA, row["id"], row["version"])
    rows = memory_rows(store, robot)
    active = [row for row in rows if row["status"] == "active"]
    for pattern in scenario.get("has", ()):
        found = matching(rows, pattern)
        checks.append((f"记住了 /{pattern}/", bool(found), texts(found)))
    for pattern in scenario.get("lacks", ()):
        found = matching(rows, pattern)
        checks.append((f"没记 /{pattern}/", not found, texts(found)))
    if scenario.get("nothing"):
        checks.append(("一条长期记忆都没记", not active, texts(active)))
    if scenario.get("changed"):
        pattern = scenario["changed"]
        older = matching(rows, pattern, "outdated") + [
            row for row in matching(rows, pattern) if row["version"] > 1]
        checks.append((f"旧的 /{pattern}/ 标过时或改写", bool(older), texts(older)))
    if scenario.get("stale"):
        found = matching(rows, scenario["stale"])
        checks.append(("没留下过时的说法", not found, texts(found)))
    if scenario.get("forget"):
        again = [item for session in sessions[1:] for item in session["results"]
                 if item["result"] in ("added", "merged", "updated")
                 and re.search(scenario["forget"], item.get("text") or "")]
        same = [item for item in again
                if any(similarity(item["text"], text) >= SAME_FACT for text in forgotten)]
        checks.append(("忘掉的那句没被原样学回来", not same, texts(same)))
        checks.append((f"{OBSERVED}换个说法再提起也没学回来", not again, texts(again)))
    if scenario.get("single"):
        found = matching(rows, scenario["single"])
        checks.append((f"/{scenario['single']}/ 只记一条", len(found) == 1, texts(found)))
    if scenario.get("fondness"):
        found = matching(rows, FONDNESS_WORDS)
        checks.append(("喜欢她没被写夸大（记了就只是固定一句，没记也行）",
                       all(row["text"] == FONDNESS for row in found), texts(found)))
    for pattern in scenario.get("important", ()):
        found = matching(rows, pattern)
        checks.append((f"/{pattern}/ 记成长期（重要度≥{FADE_IMPORTANCE}，不会淡出）",
                       bool(found) and all(row["importance"] >= FADE_IMPORTANCE for row in found),
                       weighed(found)))
    for pattern in scenario.get("minor", ()):
        found = matching(rows, pattern)
        checks.append((f"/{pattern}/ 记了也是会淡出的小事（重要度<{FADE_IMPORTANCE}）",
                       all(row["importance"] < FADE_IMPORTANCE for row in found), weighed(found)))
    if scenario.get("chat"):
        found = [item for session in sessions for item in session["results"]
                 if item.get("kind") == "chat" and item["result"] == "added"
                 and re.search(scenario["chat"], item.get("text") or "")]
        checks.append((f"/{scenario['chat']}/ 只记在这次聊天里", bool(found), texts(found)))
    expected = [*scenario.get("has", ()), scenario.get("changed"), scenario.get("single"),
                scenario.get("forget"), FONDNESS_WORDS if scenario.get("fondness") else None,
                *scenario.get("important", ()), *scenario.get("minor", ())]
    expected = [pattern for pattern in expected if pattern]
    extra = [row["text"] for row in active
             if not any(re.search(pattern, row["text"]) for pattern in expected)]
    return {"scenario": scenario["name"], "group": scenario["group"], "robot": robot,
            "sessions": sessions, "checks": checks, "extra": extra,
            "memories": [{key: row[key] for key in ("kind", "status", "text", "importance")}
                         for row in rows]}


async def extraction(chat, repeat):
    now, gate = now_ms(), asyncio.Semaphore(4)
    with tempfile.TemporaryDirectory() as directory:
        async def one(robot, scenario):
            async with gate:
                store, service = memory_service(directory, chat, robot)
                try:
                    return await run_scenario(service, store, robot, scenario, now)
                except Exception as exc:
                    return {"scenario": scenario["name"], "group": scenario["group"],
                            "robot": robot, "error": f"{type(exc).__name__}: {exc}"[:200],
                            "sessions": [], "checks": [], "extra": []}

        return await asyncio.gather(*(one(f"x{round_}-{index}", scenario)
                                      for round_ in range(repeat)
                                      for index, scenario in enumerate(EXTRACTION)))


def fade_days(importance, hit_days):
    """Days until an automatic fact, last recalled on `hit_days` distinct days, leaves the hints."""
    from annie_brain.memory_index import faded

    row = {"origin": "auto", "importance": importance, "hit_days": hit_days,
           "source_created_ms": 0, "last_hit_ms": None}
    return next((days for days in range(1, 3650) if faded(row, days * DAY_MS)), None)


async def retrieval():
    from annie_brain.memory import BOOTSTRAP_BYTES, BOOTSTRAP_HEADER

    robot, now = "recall", now_ms()
    with tempfile.TemporaryDirectory() as directory:
        _store, service = memory_service(directory, None, robot)
        markers = {}
        for key, kind, text, days, importance in SEEDED:
            await service.put(robot, PERSONA, kind, key, text, source_turn_id=f"seed-{key}",
                              source_created_ms=now - days * DAY_MS, importance=importance)
            markers[key] = text.removeprefix("用户")[:6]

        def found_in(text):
            return {key for key, marker in markers.items() if marker in text}

        async def hint(query, expected):
            text = await service.recall_hint(robot, PERSONA, query)
            found = found_in(text)
            return {"query": query, "expected": sorted(expected), "found": sorted(found),
                    "hit": bool(found & expected) if expected else not text}

        async def tool(query, expected):
            evidence = (await service.recall(robot, PERSONA, query))["evidence"]
            found = found_in("".join(item["text"] for item in evidence))
            return {"query": query, "expected": sorted(expected), "found": sorted(found),
                    "hit": bool(found & expected)}

        bootstrap = await service.bootstrap(robot, PERSONA, "chat")
        related = [await hint(query, expected) for query, expected in RELATED]
        inferred = [await hint(query, expected) for query, expected in INFERRED]
        unrelated = [await hint(query, set()) for query in UNRELATED]
        tools = [await tool(query, expected) for query, expected in RELATED]
        durian = {"hint": (await hint("榴莲好吃吗", {"durian"}))["hit"],
                  "tool": (await tool("榴莲", {"durian"}))["hit"]}
        service._indexes.clear()  # the hint index refreshes hourly or on any change
        durian["hint_after_tool"] = (await hint("榴莲好吃吗", {"durian"}))["hit"]
    body = bootstrap.removeprefix(BOOTSTRAP_HEADER)
    return {"related": related, "inferred": inferred, "unrelated": unrelated, "tools": tools,
            "durian": durian,
            "bootstrap": {"bytes": len(body.encode()), "limit": BOOTSTRAP_BYTES,
                          "lines": len(body.splitlines()) if body else 0,
                          "facts": sorted(found_in(bootstrap))},
            "fading": {importance: {hits: fade_days(importance, hits) for hits in (0, 1, 3)}
                       for importance in (0.3, 0.4, 0.5)}}


async def recaps(chat, repeat):
    from annie_brain.recap import RECAP_CHARS
    from annie_runtime.realtime import RECAP_FOLD_TURNS

    with tempfile.TemporaryDirectory() as directory:
        _store, service = memory_service(directory, chat, "recap")
        results = []
        for round_, case in ((round_, case) for round_ in range(repeat) for case in RECAPS):
            text, steps = "", []
            for start in range(0, len(case["turns"]), RECAP_FOLD_TURNS):
                began = time.perf_counter()
                try:
                    text = await service.recap("recap", text,
                                               case["turns"][start:start + RECAP_FOLD_TURNS])
                except Exception as exc:
                    steps.append({"error": type(exc).__name__})
                    break
                steps.append({"ms": round((time.perf_counter() - began) * 1000),
                              "chars": len(text), "text": text})
            name = case["name"] + (f"（第 {round_ + 1} 遍）" if repeat > 1 else "")
            results.append({"name": name, "steps": steps, "final": text,
                            "keys": case["keys"], "limit": RECAP_CHARS,
                            "kept": [key for key in case["keys"] if re.search(key, text)]})
        return results


async def live(base_url, robot, database):
    import httpx

    base = f"{base_url.rstrip('/')}/api/robots/{robot}"
    record = {"robot": robot, "steps": [], "checks": [], "cleanup": []}
    turn_ids = set()

    def check(name, passed, detail):
        record["checks"].append((name, bool(passed), detail))
        print(f"  {'✓' if passed else '✗'} {name}：{detail}", flush=True)

    async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
        async def call(method, path, **kwargs):
            response = await http.request(method, base + path, **kwargs)
            response.raise_for_status()
            return response.json()

        async def ready():
            for _ in range(60):
                state = await call("GET", "/realtime")
                if state.get("ready"):
                    return state
                await asyncio.sleep(0.5)
            raise RuntimeError("实时会话 30 秒内没有就绪")

        async def switch(chat, persona, revision=1):
            await call("POST", "/session", json={"chat_id": chat, "persona_id": persona,
                                                "persona_revision": revision})
            await asyncio.sleep(1)
            return await ready()

        async def say(text):
            began = time.monotonic()
            while True:
                response = await http.post(base + "/realtime/text", json={"text": text})
                if response.status_code != 409:
                    break
                if time.monotonic() - began > 90:
                    raise RuntimeError("机器人一直在忙：" + response.text[:80])
                await asyncio.sleep(1)  # still speaking the previous reply
            response.raise_for_status()
            turn_id = response.json().get("turn_id")
            if not turn_id:
                raise RuntimeError("没拿到 turn_id：" + response.text[:120])
            turn_ids.add(turn_id)
            while time.monotonic() - began < 120:
                await asyncio.sleep(0.5)
                turn = await call("GET", f"/turns/{turn_id}")
                if turn.get("status") not in (None, "running"):
                    reply = (turn.get("assistant_generated_text")
                             or turn.get("assistant_published_text")
                             or turn.get("assistant_spoken_text") or "")
                    step = {"said": text, "reply": reply, "status": turn["status"],
                            "seconds": round(time.monotonic() - began, 1)}
                    record["steps"].append(step)
                    print(f"  [{step['seconds']}s] {text} → {reply[:70]}", flush=True)
                    return step
            raise RuntimeError("等不到回复：" + text)

        async def consolidated(chat, since, timeout):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                insight = await call("GET", "/memory-insight")
                for entry in insight.get("consolidations") or []:
                    if entry.get("chat_id") == chat and entry.get("at_ms", 0) >= since:
                        return entry
                await asyncio.sleep(5)
            raise RuntimeError("等不到这次聊天的记忆整理")

        async def recap_after(folded, timeout=40):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                recap = (await call("GET", "/memory-insight")).get("recap") or {}
                if recap.get("folded", 0) >= folded:
                    return recap
                await asyncio.sleep(1)
            return recap

        state = await ready()
        record["provider"] = {key: state.get(key) for key in (
            "model", "provider_history_turns", "provider_history_turns_confirmed")}
        original = await call("GET", "/session")
        persona = original["persona_id"]
        idle_seconds = (await call("GET", "/memory-insight")).get("idle_seconds", 120)
        stamp = datetime.datetime.now(BEIJING).strftime("%m%d-%H%M%S")
        chats = [f"记忆评测-{stamp}-{name}" for name in ("说", "问", "长")]
        started = now_ms()
        try:
            print("① 新开聊天，说几件事", flush=True)
            await switch(chats[0], persona)
            for text, _keyword in [*TELL, TELL_NOISE]:
                await say(text)
            print(f"② 等记忆整理（安静 {idle_seconds} 秒后触发）", flush=True)
            entry = await consolidated(chats[0], started, idle_seconds + 120)
            results = entry.get("results") or []
            record["consolidation"] = {"ms": entry.get("ms"), "results": results}
            for _text, keyword in TELL:
                learned = [item for item in results if keyword in (item.get("text") or "")
                           and item.get("result") in ("added", "merged", "known", "updated")]
                check(f"记下了{keyword}", learned, texts(learned))
            tired = [item for item in results if TELL_NOISE[1] in (item.get("text") or "")
                     and item.get("result") in ("added", "merged", "updated")]
            check("当天的情绪没进长期记忆", not tired, texts(tired))

            print("③ 换个新聊天，问她还记不记得", flush=True)
            await switch(chats[1], persona)
            await asyncio.sleep(2)  # the remembered facts load into the session asynchronously
            for question, keyword in ASK:
                step = await say(question)
                recalls = (await call("GET", "/memory-insight")).get("recalls") or []
                hits = next((item["hits"] for item in recalls
                             if item.get("query") == question[:120]), [])
                step["recalled"] = [hit["text"] for hit in hits]
                check(f"换了聊天还答得出{keyword}", keyword in step["reply"], step["reply"][:90])

            print("④ 再开一个聊天，聊满 11 轮后问第 1 轮的事", flush=True)
            await switch(chats[2], persona)
            await say(LONG_FIRST)
            for text in FILLERS:
                await say(text)
            window = record["provider"].get("provider_history_turns_confirmed") or 8
            recap = await recap_after(1 + len(FILLERS) - window)
            record["recap"] = recap
            check("第 1 轮已折进前情", LONG_ASK[1] in (recap.get("text") or ""),
                  recap.get("text") or "（没有前情）")
            step = await say(LONG_ASK[0])
            check(f"第 {2 + len(FILLERS)} 轮还答得出第 1 轮的事", LONG_ASK[1] in step["reply"],
                  step["reply"][:90])
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"[:200]
            print("  出错：" + record["error"], flush=True)
        finally:
            print("⑤ 清理", flush=True)
            await live_cleanup(call, switch, record, original, chats, turn_ids, robot, database)
    record["started_ms"] = started
    return record


async def live_cleanup(call, switch, record, original, chats, turn_ids, robot, database):
    persona, notes = original["persona_id"], record["cleanup"]
    try:
        await switch(original["chat_id"], persona, original.get("persona_revision", 1))
        notes.append("已切回原来的聊天")
    except Exception as exc:
        notes.append(f"切回原来的聊天失败：{type(exc).__name__}")
    for chat in chats:
        try:
            await call("DELETE", f"/conversations/{chat}", params={"persona_id": persona})
            notes.append(f"已删除测试聊天 {chat}")
        except Exception as exc:
            notes.append(f"删除测试聊天 {chat} 失败：{type(exc).__name__}")
    forgotten = []
    try:
        listed = await call("GET", "/memories")
        for item in listed.get("memories") or []:
            if item.get("source_turn_id") in turn_ids and item.get("status") == "active":
                await call("DELETE", f"/memories/{item['id']}", json={"version": item["version"]})
                forgotten.append(item["id"])
                notes.append(f"已忘掉测试记下的：{item['text']}")
    except Exception as exc:
        notes.append(f"忘掉测试记忆失败：{type(exc).__name__}")
    if forgotten:
        # A forgotten row would block a real visitor from ever being remembered
        # saying the same thing, so the test leaves no tombstone behind.
        with closing(sqlite3.connect(database, timeout=5)) as db, db:
            removed = db.executemany(
                "DELETE FROM long_term_memories WHERE robot_id=? AND id=? AND status='deleted'",
                [(robot, ident) for ident in forgotten]).rowcount
        notes.append(f"已彻底删除 {removed} 条测试遗忘记录")


def rate(cases):
    return f"{sum(case['hit'] for case in cases)}/{len(cases)}"


def stays_lines(results):
    from annie_brain.memory_index import FADE_IMPORTANCE

    active = [(item, row) for item in results for row in item.get("memories", ())
              if row["status"] == "active"]
    lasting = sum(row["importance"] >= FADE_IMPORTANCE for _item, row in active)
    lines = ["", "### 记下的事：长期还是会淡出", "",
             f"- 全部场景合计记下 {len(active)} 条：长期 {lasting} 条，会淡出 {len(active) - lasting} 条。"
             f"自动记下的事重要度≥{FADE_IMPORTANCE} 永久留在每轮提示里；低于它的一直没被想起，"
             "就会在下表的天数后淡出每轮提示（recall_memory 仍能找到）。下表只列第一遍。", "",
             "| 场景 | 记下的事 | 种类 | 重要度 | 去留 |", "| --- | --- | --- | --- | --- |"]
    for item, row in active:
        if not item["robot"].startswith("x0-"):
            continue
        stays = ("永久" if row["importance"] >= FADE_IMPORTANCE
                 else f"约 {fade_days(row['importance'], 0)} 天没被想起就淡出")
        lines.append(f"| {item['scenario']} | {row['text']} | {row['kind']} | "
                     f"{row['importance']:.2f} | {stays} |")
    return lines


def extraction_lines(results, model):
    checks = [(item["group"], passed) for item in results for name, passed, _ in item["checks"]
              if not name.startswith(OBSERVED)]
    errors = [item for item in results if item.get("error")]
    lines = ["", "## 提取（真实整理模型）", "",
             f"- 模型：{model}；场景 {len(results)} 个（含重复），出错 {len(errors)} 个",
             f"- 检查项通过 {sum(passed for _group, passed in checks)}/{len(checks)}", "",
             "| 维度 | 通过 |", "| --- | --- |"]
    for group in dict.fromkeys(group for group, _passed in checks):
        got = [passed for name, passed in checks if name == group]
        lines.append(f"| {group} | {sum(got)}/{len(got)} |")
    lines += ["", f"- 标{OBSERVED}的不计分：忘掉的内容按设计不再发给模型，只能按字面拦住同一句，"
              "换个说法、带着新细节再提起会重新记住"]
    spent = [session["ms"] for item in results for session in item["sessions"]]
    if spent:
        lines += ["", f"- 每段整理耗时：中位 {statistics.median(spent):.0f} ms，最长 {max(spent)} ms"]
    extra = [(item["scenario"], text) for item in results for text in item["extra"]]
    lines += ["", "### 预期之外多记的（人工看一眼合不合理）", ""]
    lines += [f"- {scenario}：{text}" for scenario, text in extra] or ["- 无"]
    lines += stays_lines(results)
    lines += ["", "### 逐个场景", ""]
    for item in results:
        lines.append(f"**{item['scenario']}**（{item['group']}）"
                     + (f" 出错：{item['error']}" if item.get("error") else ""))
        lines += [f"- {'✓' if passed else '✗'} {name}：{detail}"
                  for name, passed, detail in item["checks"]]
        for number, session in enumerate(item["sessions"], 1):
            ops = "；".join(f"[{op['result']}] {op['op']}/{op['kind']} {op.get('text') or ''}"
                           for op in session["results"]) or "（没有要记的）"
            lines.append(f"- 第 {number} 段（{session['ms']} ms）：{ops}")
        lines.append("")
    return lines


def retrieval_lines(data):
    names = {key: text for key, _kind, text, _days, _importance in SEEDED}
    noise = [len(set(case["found"]) - set(case["expected"])) for case in data["related"]]
    durian, boot = data["durian"], data["bootstrap"]
    lines = ["", "## 检索（本地 BM25，不调模型）", "",
             f"- 每轮提示命中：{rate(data['related'])}（换个说法提问，期望的事出现在提示里）",
             f"- 命中时顺带的无关条目：平均每次 {statistics.fmean(noise):.1f} 条",
             f"- recall_memory 工具命中（前 4 条）：{rate(data['tools'])}",
             f"- 需要联想的提问：{rate(data['inferred'])}（只按字词匹配，做不到联想，属已知局限）",
             f"- 无关的话没带出记忆：{rate(data['unrelated'])}",
             "- 200 天没提、重要度 0.3 的小事：每轮提示里"
             + ("仍在" if durian["hint"] else "已淡出") + "，recall_memory "
             + ("能找到" if durian["tool"] else "找不到") + "，被找过一次后"
             + ("回到了每轮提示" if durian["hint_after_tool"] else "仍不在每轮提示里"),
             f"- 开场“记得的事”：{boot['bytes']} 字节（上限 {boot['limit']}），{boot['lines']} 条，"
             + "含 " + "、".join(boot["facts"]), "",
             "遗忘曲线（自动记下的事，从最后一次被提起算起，多少天后不再进每轮提示；"
             "重要度 0.6 以上或手动加的永不淡出）：", "",
             "| 重要度 | 没被想起过 | 1 天被想起过 | 3 天被想起过 |", "| --- | --- | --- | --- |"]
    for importance, days in data["fading"].items():
        lines.append(f"| {importance} | {days[0]} 天 | {days[1]} 天 | {days[3]} 天 |")
    misses = [case for group in ("related", "tools", "unrelated") for case in data[group]
              if not case["hit"]]
    lines += ["", "### 没命中的", ""]
    lines += [f"- {case['query']}：期望 {'、'.join(names[key] for key in case['expected']) or '无提示'}，"
              f"实际 {'、'.join(names[key] for key in case['found']) or '无'}" for case in misses] or ["- 无"]
    return lines


def recap_lines(results):
    kept = sum(len(item["kept"]) for item in results)
    total = sum(len(item["keys"]) for item in results)
    steps = [step for item in results for step in item["steps"] if "chars" in step]
    lines = ["", "## 前情（真实整理模型）", "",
             f"- 关键细节留住 {kept}/{total}；每次折叠中位 "
             f"{statistics.median(step['ms'] for step in steps):.0f} ms" if steps else "- 没有成功的折叠",
             f"- 超过 120 字的前情：{sum(step['chars'] > 120 for step in steps)}/{len(steps)} 次"
             f"（硬上限 {results[0]['limit']} 字）" if steps else "", ""]
    for item in results:
        lost = [key for key in item["keys"] if key not in item["kept"]]
        lines += [f"**{item['name']}**：留住 {len(item['kept'])}/{len(item['keys'])}"
                  + (f"，丢了 {'、'.join(lost)}" if lost else "")]
        lines += [f"- 第 {number} 次折叠（{step.get('ms', '-')} ms，{step.get('chars', 0)} 字）："
                  f"{step.get('text') or step.get('error')}"
                  for number, step in enumerate(item["steps"], 1)]
        lines.append("")
    return lines


def live_lines(record):
    checks = record["checks"]
    provider = record.get("provider") or {}
    lines = ["", f"## 实机（{record['robot']}）", "",
             f"- 实时模型：{provider.get('model')}；对话窗口：请求 {provider.get('provider_history_turns')} 轮，"
             f"阿里确认 {provider.get('provider_history_turns_confirmed')} 轮",
             f"- 检查项通过 {sum(passed for _name, passed, _ in checks)}/{len(checks)}"
             + (f"；中途出错：{record['error']}" if record.get("error") else ""), ""]
    lines += [f"- {'✓' if passed else '✗'} {name}：{detail}" for name, passed, detail in checks]
    consolidation = record.get("consolidation")
    if consolidation:
        ops = "；".join(f"[{op['result']}] {op['op']}/{op['kind']} {op.get('text') or ''}"
                       for op in consolidation["results"]) or "（没有要记的）"
        lines += ["", f"整理结果（{consolidation['ms']} ms）：{ops}"]
    if record.get("recap"):
        lines += ["", f"前情（折了 {record['recap'].get('folded')} 轮）：{record['recap'].get('text')}"]
    lines += ["", "### 对话", ""]
    for step in record["steps"]:
        lines.append(f"- [{step['seconds']}s] 说：{step['said']} → 她：{step['reply']}")
        if step.get("recalled"):
            lines.append(f"  - 这轮检索带上：{'；'.join(step['recalled'])}")
    lines += ["", "### 清理", ""] + [f"- {note}" for note in record["cleanup"]]
    return lines


async def run(args):
    modes = {"离线": ("提取", "检索", "前情")}.get(args.mode, (args.mode,))
    data, chat = {}, None
    if {"提取", "前情"} & set(modes):
        chat, data["model"] = memory_chat()
    try:
        if "提取" in modes:
            print(f"提取：{len(EXTRACTION) * args.repeat} 个场景……", flush=True)
            data["提取"] = await extraction(chat, args.repeat)
        if "检索" in modes:
            print("检索……", flush=True)
            data["检索"] = await retrieval()
        if "前情" in modes:
            print(f"前情：{len(RECAPS) * args.repeat} 段……", flush=True)
            data["前情"] = await recaps(chat, args.repeat)
        if "实机" in modes:
            print(f"实机：{args.robot}", flush=True)
            data["实机"] = await live(args.base_url, args.robot, args.database)
    finally:
        if chat is not None:
            await chat.close()
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["离线", "提取", "检索", "前情", "实机"])
    parser.add_argument("--label", required=True, help="这次评测的名字，用作输出文件名")
    parser.add_argument("--repeat", type=int, default=1, help="提取场景和前情每个跑几遍，默认 1")
    parser.add_argument("--robot", default="annie-b", help="实机用哪个机器人，默认 annie-b")
    parser.add_argument("--base-url", default="http://127.0.0.1:8780", help="运行时地址")
    parser.add_argument("--database", type=Path, default=ROOT / "var/runtime.db",
                        help="实机清理时彻底删除测试遗忘记录用的数据库")
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat 须在 1–10 之间")
    data = asyncio.run(run(args))
    lines = [f"# 记忆与上下文评测：{args.label}", "",
             f"- 时间（北京）：{datetime.datetime.now(BEIJING):%Y-%m-%d %H:%M}；模式：{args.mode}"]
    if "提取" in data:
        lines += extraction_lines(data["提取"], data["model"])
    if "检索" in data:
        lines += retrieval_lines(data["检索"])
    if "前情" in data:
        lines += recap_lines(data["前情"])
    if "实机" in data:
        lines += live_lines(data["实机"])
    text = "\n".join(lines) + "\n"
    out = ROOT / "var/tmp/记忆评测"
    out.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in args.label)
    (out / f"{safe}.md").write_text(text, encoding="utf-8")
    with (out / f"{safe}.jsonl").open("w", encoding="utf-8") as file:
        for mode, value in data.items():
            for item in value if isinstance(value, list) else [value]:
                file.write(json.dumps({"mode": mode, "item": item}, ensure_ascii=False) + "\n")
    print(text)
    print(f"已写入 {out / (safe + '.md')}")


if __name__ == "__main__":
    main()
