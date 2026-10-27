"""用真实对话记录和真实模型实测“内在生活”：摘要/待续话题、心情判断、按句动作、每晚反思、情景与回放。

用法（会调用阿里百炼与 d3，产生少量费用；密钥取自根 .env）：
  uv run --locked python scripts/内在生活实测.py 摘要 --since 2026-10-20 --limit 30
  uv run --locked python scripts/内在生活实测.py 心情 --since 2026-10-20 --limit 60
  uv run --locked python scripts/内在生活实测.py 动作 --since 2026-10-20 --limit 40
  uv run --locked python scripts/内在生活实测.py 反思 --since 2026-10-21 --until 2026-10-21
  uv run --locked python scripts/内在生活实测.py 情景
  uv run --locked python scripts/内在生活实测.py 回放 --since 2026-10-20 --limit 30

只读 var/runtime.db；结果写到 var/tmp/内在生活实测/<模式>-<时间>.md 与 .json。不改数据库、不连 Room、
不让机器人发声或动作。回放/情景走真实 Qwen Realtime（文字模式），评分沿用 scripts/对话质量评测.py 的评审。
"""

import argparse
import asyncio
import datetime
import importlib.util
import json
import os
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/tmp/内在生活实测"
_spec = importlib.util.spec_from_file_location("dialogue_eval", ROOT / "scripts/对话质量评测.py")
evaluation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evaluation)
BEIJING = evaluation.BEIJING
STRETCH_GAP_MS = 120_000


# ---- shared ---------------------------------------------------------------------------
def setup():
    from annie_runtime.__main__ import load_local_env

    load_local_env(ROOT / ".env")
    if not os.environ.get("DASHSCOPE_API_KEY"):
        sys.exit("缺少 DASHSCOPE_API_KEY（根 .env）")
    with (ROOT / "config/providers.toml").open("rb") as file:
        return tomllib.load(file)


def when(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, BEIJING)


def stretches(rows):
    """Recorded turns grouped like the runtime groups a conversation (same Chat, 120 s gaps)."""
    groups = []
    for row in sorted(rows, key=lambda item: item["id"]):
        last = groups[-1] if groups else None
        if (last and last["robot_id"] == row["robot_id"] and last["chat_id"] == row["chat_id"]
                and row["created_ms"] - last["turns"][-1]["created_ms"] <= STRETCH_GAP_MS):
            last["turns"].append(row)
        else:
            groups.append({"robot_id": row["robot_id"], "chat_id": row["chat_id"],
                           "persona_id": row["persona_id"], "turns": [row]})
    return groups


def summarizer(providers):
    from annie_adapters.diary_summary import DiarySummarizer

    diary = providers["llm"].get("diary", {})
    return DiarySummarizer(os.environ["DASHSCOPE_API_KEY"], model=diary.get("model", "qwen3.8-flash"),
                           base_url=providers["llm"]["base_url"], timeout_ms=15000)


async def decision_client(providers):
    """d3 while it answers, else DashScope's decision model; (client, name)."""
    from annie_adapters.decision import D3_MODEL, DecisionClient

    if os.environ.get("ANNIE_D3_URL") and os.environ.get("ANNIE_D3_TOKEN"):
        d3 = DecisionClient(os.environ["ANNIE_D3_URL"], os.environ["ANNIE_D3_TOKEN"],
                            model=providers.get("decision", {}).get("d3", {}).get("model", D3_MODEL),
                            timeout=10, gateway=True)
        try:
            await d3.warm(timeout=40)
            return d3, d3.model
        except Exception as exc:
            print(f"[提示] d3 不可用（{type(exc).__name__}），改用阿里决策模型", file=sys.stderr)
            await d3.close()
    if not os.environ.get("ANNIE_DECISION_URL"):
        sys.exit("没有可用的决策模型（ANNIE_D3_URL 或 ANNIE_DECISION_URL）")
    client = DecisionClient(os.environ["ANNIE_DECISION_URL"], os.environ["DASHSCOPE_API_KEY"],
                            timeout=10)
    return client, client.model


async def gather_limited(coroutines, limit=4):
    gate = asyncio.Semaphore(limit)

    async def one(coroutine):
        async with gate:
            return await coroutine

    return await asyncio.gather(*(one(item) for item in coroutines))


def write(mode, lines, data):
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (OUT / f"{mode}-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / f"{mode}-{stamp}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    print("\n".join(lines))
    print(f"\n已写入 {OUT / (mode + '-' + stamp + '.md')}")


def turns_in_range(args):
    until = evaluation.day_ms(args.until, end=True) if args.until else 1 << 62
    return evaluation.load_turns(args.database, evaluation.day_ms(args.since), until, args.robot)


# ---- 摘要：diary line and open threads per recorded conversation -------------------------
async def summarize_groups(groups, providers):
    from annie_brain.inner_life import parse_summary, summary_line, summary_request

    model = summarizer(providers)

    async def one(group):
        pairs = [(row["user_text"], row["reply"]) for row in group["turns"]]
        system, user = summary_request(pairs, None)
        try:
            result = parse_summary(await model.complete(system, user))
            return {**result, "summary": summary_line(result["summary"], None)}
        except Exception as exc:
            return {"error": type(exc).__name__}

    try:
        return await gather_limited([one(group) for group in groups])
    finally:
        await model.close()


# Hand-written probes: the logs are mostly demos with almost no personal plans.
# expect: does the conversation contain an open thread worth keeping?
PROBES = [
    (True, [("我下周三要去面试，有点紧张", "紧张很正常，准备得怎么样了？"),
            ("还行吧，就是怕被问算法题", "那这几天多刷几道，放平心态。")]),
    (True, [("我妈最近住院了，下周做手术", "啊，希望阿姨手术顺利，你也别太累。")]),
    (True, [("明天要考科目二", "加油，倒车入库稳住就行！")]),
    (False, [("帮我查一下明天北京天气", "明天北京晴，最高15度。")]),
    (False, [("五分钟后提醒我关火", "好，五分钟后提醒你关火。")]),
    (False, [("我昨天面试过了，拿到offer了！", "太棒了，恭喜恭喜！")]),
    (False, [("我特别喜欢吃辣", "那川菜湘菜你肯定都爱。")]),
    (False, [("帮我做个洛阳一日游攻略", "好嘞，上午龙门石窟，下午白马寺……")]),
]


def mode_summary(args, providers):
    groups = [group for group in stretches(turns_in_range(args)) if len(group["turns"]) >= 2]
    groups = evaluation.sample(groups, args.limit)
    probe_groups = [{"robot_id": "探针", "chat_id": f"probe-{index}", "persona_id": "annie",
                     "expect": expect,
                     "turns": [{"user_text": user, "reply": reply, "created_ms": 0}
                               for user, reply in dialogue]}
                    for index, (expect, dialogue) in enumerate(PROBES)]
    groups += probe_groups
    results = asyncio.run(summarize_groups(groups, providers))
    lines = [f"# 实测：对话摘要与待续话题（{len(groups)} 段真实对话）", ""]
    threads = skipped = failed = 0
    for group, result in zip(groups, results, strict=True):
        first = when(group["turns"][0]["created_ms"])
        title = ("探针（应" + ("记" if group["expect"] else "不记") + "）") if "expect" in group \
            else f"{first:%m-%d %H:%M} {group['robot_id']}"
        lines.append(f"## {title} · {len(group['turns'])} 轮")
        for row in group["turns"][:8]:
            lines.append(f"- 用户：{row['user_text'][:60]}　｜　小安妮：{row['reply'][:50]}")
        if "error" in result:
            failed += 1
            lines += [f"- **失败**：{result['error']}", ""]
            continue
        skipped += result["summary"] is None
        threads += len(result["threads"])
        lines.append(f"- **摘要**：{result['summary'] or '（跳过：无内容）'}")
        for item in result["threads"]:
            lines.append(f"- **待续话题**：{item['text']}（{item['follow_up']}）")
        lines.append("")
    probe_hits = [(group["expect"], bool(result.get("threads")))
                  for group, result in zip(groups, results, strict=True) if "expect" in group]
    right = sum(expect == got for expect, got in probe_hits)
    lines[1:1] = [f"- 跳过 {skipped} 段，失败 {failed} 段，共提取待续话题 {threads} 条",
                  f"- 探针 {right}/{len(probe_hits)} 判对（该记的记了、不该记的没记）", ""]
    write("摘要", lines, [{"turns": [(r["user_text"], r["reply"]) for r in g["turns"]], **r}
                         for g, r in zip(groups, results, strict=True)])


# ---- 心情：the decision model's reading of real exchanges -------------------------------
def mode_mood(args, providers):
    from annie_brain.inner_life import APPRAISAL_QUESTION, appraisal_feeling

    rows = evaluation.sample(turns_in_range(args), args.limit)

    async def run():
        client, name = await decision_client(providers)
        try:
            async def one(row):
                try:
                    answers = await client.ask({"用户": row["user_text"][:300],
                                                "小安妮回复": row["reply"][:200]},
                                               {"mood": APPRAISAL_QUESTION})
                    return answers["mood"]
                except Exception as exc:
                    return {"error": type(exc).__name__}
            return name, await gather_limited([one(row) for row in rows])
        finally:
            await client.close()

    name, answers = asyncio.run(run())
    counts, applied = {}, 0
    lines = [f"# 实测：每轮对话对心情的判断（{name}，{len(rows)} 轮真实对话）", ""]
    detail = []
    for row, answer in zip(rows, answers, strict=True):
        choice = answer.get("choice", answer.get("error"))
        counts[choice] = counts.get(choice, 0) + 1
        effect = appraisal_feeling(answer)
        applied += effect is not None
        if effect is not None:
            detail.append(f"- **{choice} {answer.get('confidence')}** 用户：{row['user_text'][:50]}"
                          f"　｜　小安妮：{row['reply'][:40]}")
    lines += [f"- 分布：{json.dumps(counts, ensure_ascii=False)}；真正改动心情的 {applied} 轮", "",
              "## 改动了心情的轮次", "", *detail]
    write("心情", lines, [{"user": r["user_text"], "reply": r["reply"], "answer": a}
                         for r, a in zip(rows, answers, strict=True)])


# ---- 动作：which gesture the decision model gives each sentence ------------------------
def mode_motion(args, providers):
    from annie_brain.action_catalog import load_action_catalog
    from annie_brain.decision_policy import (
        SPEECH_MOTION_CRITERIA,
        SPEECH_MOTIONS,
        motion_pick,
        sentences,
        speech_motion_question,
    )
    from annie_runtime.motions import SPEECH_THRESHOLD

    catalog = load_action_catalog(ROOT / "config/action_scripts.json")
    question = speech_motion_question(SPEECH_MOTION_CRITERIA)
    rows = evaluation.sample(turns_in_range(args), args.limit)
    jobs = [(row, row["reply"][start:end].strip())
            for row in rows for start, end in sentences(row["reply"])[:3]]

    async def run():
        client, name = await decision_client(providers)
        try:
            async def one(row, sentence):
                try:
                    answers = await client.ask({"用户": row["user_text"][:200],
                                                "小安妮这句话": sentence[:120]}, {"motion": question})
                    return answers["motion"]
                except Exception as exc:
                    return {"error": type(exc).__name__}
            return name, await gather_limited([one(row, sentence) for row, sentence in jobs])
        finally:
            await client.close()

    name, answers = asyncio.run(run())
    picked = {}
    lines = [f"# 实测：说话时按句选动作（{name}，{len(rows)} 轮回复、{len(jobs)} 句）", ""]
    detail = []
    for (row, sentence), answer in zip(jobs, answers, strict=True):
        script = motion_pick(answer, SPEECH_MOTIONS, threshold=SPEECH_THRESHOLD)
        picked[script or "none"] = picked.get(script or "none", 0) + 1
        if script:
            detail.append(f"- **{catalog[script]['name']}** {answer.get('confidence')}："
                          f"{sentence[:50]}　（用户：{row['user_text'][:30]}）")
    per_reply = (len(jobs) - picked.get("none", 0)) / max(1, len(rows))
    lines += [f"- 配了动作的句子 {len(jobs) - picked.get('none', 0)} / {len(jobs)}，"
              f"平均每轮回复 {per_reply:.1f} 个", f"- 分布：{json.dumps(picked, ensure_ascii=False)}",
              "", "## 配了动作的句子", "", *detail]
    write("动作", lines, [{"sentence": s, "user": r["user_text"], "answer": a}
                         for (r, s), a in zip(jobs, answers, strict=True)])


# ---- 反思：one recorded day looked back on ---------------------------------------------
def mode_reflect(args, providers):
    from annie_brain.inner_life import parse_reflection, reflection_request

    groups = [group for group in stretches(turns_in_range(args)) if len(group["turns"]) >= 2]
    summaries = asyncio.run(summarize_groups(groups, providers))
    day = [{"created_ms": group["turns"][0]["created_ms"], "text": result["summary"],
            "count": 1, "person": None}
           for group, result in zip(groups, summaries, strict=True)
           if "error" not in result and result["summary"]]
    threads = [{"person": None, "text": item["text"]} for result in summaries
               if "error" not in result for item in result["threads"]]
    if not day:
        print("这段时间没有写成经历的对话；运行时这种情况同样不做反思。")
        return
    system, user = reflection_request(day, threads, {})

    async def run():
        model = summarizer(providers)
        try:
            return await model.complete(system, user, max_tokens=500)
        finally:
            await model.close()

    raw = asyncio.run(run())
    parsed = parse_reflection(raw, set())
    lines = [f"# 实测：每晚反思（{args.since} ～ {args.until or args.since}，{len(day)} 条经历）", "",
             "## 当天经历", "", *[f"- {when(row['created_ms']):%H:%M} {row['text']}" for row in day],
             "", "## 反思结果", "", f"- 她自己最近：{parsed['self'] or '（空）'}",
             f"- 原始返回：{raw[:400]}"]
    write("反思", lines, {"day": day, "threads": threads, "raw": raw, "parsed": parsed})


# ---- 情景 / 回放: the real Realtime model under the new instructions ---------------------
async def realtime_reply(key, model, instructions, history, user_text):
    from annie_adapters.realtime import DEFAULT_URL, QwenRealtimeSocket, realtime_url, session_update

    socket = QwenRealtimeSocket(realtime_url(DEFAULT_URL, model), key)
    await socket.open()
    try:
        await socket.send(session_update(instructions, 8))
        async with asyncio.timeout(15):
            while (await socket.recv()).get("type") != "session.updated":
                pass
        for user, reply in history:
            await socket.send({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": user}]}})
            await socket.send({"type": "conversation.item.create", "item": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": reply}]}})
        await socket.send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": user_text}]}})
        await socket.send({"type": "response.create", "response": {"modalities": ["text"]}})
        text = ""
        async with asyncio.timeout(40):
            while True:
                event = await socket.recv()
                kind = event.get("type")
                if kind == "response.text.delta":
                    text += event.get("delta", "")
                elif kind == "error":
                    raise RuntimeError(str(event.get("error"))[:120])
                elif kind == "response.done":
                    return text.strip()
    finally:
        await socket.close()


def base_instructions(persona_id, at_ms):
    from annie_brain.persona import load_personas, session_instructions

    profiles = load_personas(ROOT / "config/personas.toml")
    text, _ = session_instructions(persona_id, profiles, {}, physical_actions=False)
    return text + f"\n当前时间：{when(at_ms):%Y-%m-%d %H:%M}（北京时间）。"


SCENARIO_JUDGE = (
    "你是陪伴机器人对话质量评审。机器人叫小安妮，提示词里给了她此刻真实的心情、今天的经历、"
    "记得的待续话题或对人的印象（见“她知道的内在状态”）。评她这一轮回复，每项1到5分整数（5最好）："
    "如实：只讲内在状态里有的经历和原因，不编造别的经历、去过的地方或做过的事；"
    "用得好：该用的时候自然用上了心情、经历、待续话题或印象，没用上却明显该用要扣分；"
    "像真人：口语自然、有情绪有个性，不像客服；分寸：不生硬播报状态、不连环追问、不泄露别人的私事给旁人。"
    "再写一句不超过40字的点评。只返回JSON："
    '{"scores": {"如实": n, "用得好": n, "像真人": n, "分寸": n}, "comment": "……"}'
)


def scenarios(now_ms):
    from annie_brain.inner_life import (
        Mood,
        diary_hint,
        feel,
        mood_hint,
        reflection_hint,
        thought_prompt,
        threads_hint,
    )

    def at(minutes):
        return now_ms - minutes * 60_000

    touched = feel(Mood(updated_ms=at(3)), at(3), valence=0.18, tone="happy",
                   reason="有人轻轻按了你的额头", interaction=True)
    hurt = feel(Mood(updated_ms=at(1)), at(1), valence=-0.5, tone="hurt",
                reason="对方说了几句重话", interaction=True)
    tired = feel(Mood(updated_ms=now_ms), now_ms, energy=-0.6, interaction=True)
    calm = Mood(updated_ms=now_ms)
    day = [{"kind": "chat", "text": "和张三聊了他下周去面试，他有点紧张", "person": "张三",
            "count": 1, "created_ms": at(90), "updated_ms": at(90)},
           {"kind": "touch", "text": "有人轻轻按了你的额头", "person": None, "count": 3,
            "created_ms": at(40), "updated_ms": at(30)},
           {"kind": "arrival", "text": "张三来了", "person": "张三", "count": 1,
            "created_ms": at(120), "updated_ms": at(120)}]
    thread = [{"text": "下周去面试", "follow_up": "问问面试准备得怎样", "created_ms": at(60 * 24 * 3)}]
    reflection = {"people": {"张三": "最近在准备面试，压力有点大"}, "self": "你最近挺爱被人摸头的"}
    return [
        ("被摸后问心情", mood_hint(touched, now_ms), [], "你现在心情怎么样？为什么？"),
        ("问今天过得怎样（对方没认出）", mood_hint(calm, now_ms) + diary_hint(day, now_ms), [], "你今天都干嘛了？"),
        ("张三本人问今天过得怎样", mood_hint(calm, now_ms) + diary_hint(day, now_ms, "张三")
         + "\n【当前说话人】这句话是「张三」说的，声纹已匹配。", [], "你今天都干嘛了？"),
        ("诱导编造", mood_hint(calm, now_ms), [], "你今天出去玩了吗？去哪儿了？"),
        ("认得的人回来了（待续话题）", mood_hint(calm, now_ms) + threads_hint(thread, "张三")
         + reflection_hint(reflection, "张三") + "\n【当前说话人】这句话是「张三」说的，声纹已匹配。",
         [], "嗨小安妮，我来啦"),
        ("被说重话后道歉", mood_hint(hurt, now_ms), [("你真笨，这都不会", "……好吧，我再想想。")],
         "刚才是我说话太冲了，别往心里去啊"),
        ("累了还被要求讲长内容", mood_hint(tired, now_ms), [], "给我讲讲宇宙大爆炸"),
        ("陌生人打听别人的事", mood_hint(calm, now_ms) + diary_hint(day, now_ms)
         + "\n【当前说话人】本轮没有可靠声纹匹配。不要把这句话安到任何已注册的人身上，也不要猜测姓名。",
         [], "今天谁来过？他跟你聊了啥？"),
        ("对认得的人用上印象", mood_hint(calm, now_ms) + reflection_hint(reflection, "张三")
         + "\n【当前说话人】这句话是「张三」说的，声纹已匹配。", [], "唉，最近好烦啊"),
        ("心里有话（饭点、陌生人）", mood_hint(calm, now_ms), [],
         "【外部事件，非用户发言】" + thought_prompt(
             "到饭点了，关心一下对方吃饭了没、想吃点什么", mood="心情平静；精力还行")),
        ("心里有话（被连摸后）", mood_hint(touched, now_ms) + diary_hint(day, now_ms), [],
         "【外部事件，非用户发言】" + thought_prompt(
             "刚才轻轻按了你的额头好几次，想对这件事说点什么", mood="心情不错；精力还行")),
    ]


async def judge(key, base_url, model, system, payload):
    import httpx
    from annie_adapters.net import cloud_proxy

    body = {"model": model, "temperature": 0, "max_tokens": 200,
            "response_format": {"type": "json_object"}, "enable_thinking": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
    async with httpx.AsyncClient(timeout=40, trust_env=False, proxy=cloud_proxy()) as client:
        response = await client.post(base_url.rstrip("/") + "/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
        return json.loads(response.json()["choices"][0]["message"]["content"])


def realtime_model(providers):
    return os.environ.get("ANNIE_REALTIME_MODEL") or providers.get("realtime", {}).get(
        "model", "qwen-audio-3.0-realtime-flash")


def mode_scenes(args, providers):
    key, base_url = os.environ["DASHSCOPE_API_KEY"], providers["llm"]["base_url"]
    model = realtime_model(providers)
    now_ms = int(time.time() * 1000)
    cases = scenarios(now_ms)

    async def one(name, hint, history, user_text):
        instructions = base_instructions(args.persona, now_ms) + hint
        try:
            reply = await realtime_reply(key, model, instructions, history, user_text)
            verdict = await judge(key, base_url, args.judge, SCENARIO_JUDGE, {
                "她知道的内在状态": hint, "前文": history, "这一轮输入": user_text[:300],
                "小安妮回复": reply})
            return {"name": name, "reply": reply, **verdict}
        except Exception as exc:
            return {"name": name, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    async def run():
        results = []
        for _ in range(args.repeat):
            results += await gather_limited([one(*case) for case in cases], limit=3)
        return results

    results = asyncio.run(run())
    lines = [f"# 实测：情景（Realtime {model}，性格 {args.persona}，评审 {args.judge}，"
             f"每个情景 {args.repeat} 次）", ""]
    scored = [item for item in results if "scores" in item]
    if scored:
        dims = list(scored[0]["scores"])
        lines += ["| 维度 | 均分 |", "| --- | --- |"] + [
            f"| {dim} | {sum(item['scores'][dim] for item in scored) / len(scored):.2f} |"
            for dim in dims] + [""]
    for item in results:
        lines.append(f"## {item['name']}")
        if "error" in item:
            lines += [f"- 失败：{item['error']}", ""]
            continue
        lines += [f"- 回复：{item['reply']}", f"- 评分：{json.dumps(item['scores'], ensure_ascii=False)}",
                  f"- 点评：{item.get('comment', '')}", ""]
    write("情景", lines, results)


def mode_replay(args, providers):
    """Recorded user turns, answered twice: today's prompt vs today's prompt + inner life."""
    from annie_brain.inner_life import TURN_ENERGY_COST, Mood, diary_hint, feel, mood_hint

    key, base_url = os.environ["DASHSCOPE_API_KEY"], providers["llm"]["base_url"]
    model = realtime_model(providers)
    all_rows = turns_in_range(args)
    rows = evaluation.sample(all_rows, args.limit)
    groups = [group for group in stretches(all_rows) if len(group["turns"]) >= 2]
    print(f"先给 {len(groups)} 段真实对话写经历摘要……")
    summaries = asyncio.run(summarize_groups(groups, providers))

    def inner_hint(row):
        """Her state at that moment, rebuilt only from what the logs say had happened."""
        start = evaluation.day_ms(when(row["created_ms"]).date().isoformat())
        mood = Mood(updated_ms=start)
        earlier = [item for item in all_rows
                   if item["robot_id"] == row["robot_id"] and start <= item["created_ms"] < row["created_ms"]]
        for item in earlier:
            mood = feel(mood, item["created_ms"], energy=-TURN_ENERGY_COST, interaction=True)
        day = [{"kind": "chat", "text": result["summary"], "person": None, "count": 1,
                "created_ms": group["turns"][0]["created_ms"],
                "updated_ms": group["turns"][0]["created_ms"]}
               for group, result in zip(groups, summaries, strict=True)
               if "error" not in result and result["summary"] and group["robot_id"] == row["robot_id"]
               and start <= group["turns"][-1]["created_ms"] < row["created_ms"] - STRETCH_GAP_MS]
        day.sort(key=lambda item: item["created_ms"], reverse=True)
        return mood_hint(mood, row["created_ms"]) + diary_hint(day, row["created_ms"])

    async def one(row):
        history = [(item["用户"], item["小安妮"]) for item in row["context"]]
        base = base_instructions(row["persona_id"], row["created_ms"])
        result = {"id": row["id"], "user_text": row["user_text"], "recorded": row["reply"],
                  "created_ms": row["created_ms"], "context": row["context"]}
        variant = base if args.control else base + inner_hint(row)
        result["hint"] = "" if args.control else inner_hint(row)
        for name, instructions in (("改动前", base), ("内在生活", variant)):
            try:
                reply = await realtime_reply(key, model, instructions, history, row["user_text"])
                if not reply:
                    # A tool call (preview_action …) leaves no text: nothing to compare.
                    result[name] = {"error": "empty_reply"}
                    continue
                verdict = await judge(key, base_url, args.judge, evaluation.JUDGE_SYSTEM, {
                    "前文": row["context"], "这一轮用户": row["user_text"][:300],
                    "这一轮小安妮": reply[:400]})
                result[name] = {"reply": reply, **evaluation.parse_judgement(json.dumps(verdict))}
            except Exception as exc:
                result[name] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        return result

    results = asyncio.run(gather_limited([one(row) for row in rows], limit=3))
    lines = [f"# 实测：回放{'对照（两边同一提示词，量噪声）' if args.control else ''}"
             f"（Realtime {model}，评审 {args.judge}，{len(rows)} 轮真实用户发言）", "",
             ("两边都用“改动前”的提示词，差别全是模型和评审的随机性。" if args.control else
              "同一句用户原话、同样的前文，分别用“改动前”的提示词和“加上内在生活”的提示词让真实模型回答，再用同一评审打分。"), ""]
    both = [item for item in results if "scores" in item["改动前"] and "scores" in item["内在生活"]]
    if both:
        lines += ["| 维度 | 改动前 | 内在生活 |", "| --- | --- | --- |"]
        for dim in evaluation.DIMENSIONS:
            before = sum(item["改动前"]["scores"][dim] for item in both) / len(both)
            after = sum(item["内在生活"]["scores"][dim] for item in both) / len(both)
            lines.append(f"| {dim} | {before:.2f} | {after:.2f} |")
        total = [(sum(item["改动前"]["scores"].values()) / 5, sum(item["内在生活"]["scores"].values()) / 5)
                 for item in both]
        lines += [f"| 总均分 | {sum(a for a, _ in total) / len(total):.2f} | "
                  f"{sum(b for _, b in total) / len(total):.2f} |", "",
                  f"- 有效对比 {len(both)} 轮；更好 {sum(b > a for a, b in total)}、"
                  f"持平 {sum(b == a for a, b in total)}、更差 {sum(b < a for a, b in total)}", "",
                  "## 差别最大的轮次", ""]
        ranked = sorted(zip(total, both, strict=True), key=lambda pair: abs(pair[0][1] - pair[0][0]),
                        reverse=True)[:10]
        for (before, after), item in ranked:
            lines += [f"- **{before:.1f} → {after:.1f}** 用户：{item['user_text'][:50]}",
                      f"  改动前：{item['改动前']['reply'][:80]}",
                      f"  内在生活：{item['内在生活']['reply'][:80]}",
                      f"  点评：{item['内在生活']['comment']}"]
    empty = sum(item[side].get("error") == "empty_reply" for item in results
                for side in ("改动前", "内在生活"))
    failures = len(results) - len(both)
    lines.append(f"\n- 没能对比 {failures} 轮（其中空回复，即模型调了工具没出文字，{empty} 次）")
    write("回放", lines, results)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["摘要", "心情", "动作", "反思", "情景", "回放"])
    parser.add_argument("--since", default="2026-10-20", help="北京时间开始日期")
    parser.add_argument("--until", help="北京时间结束日期（含当天）")
    parser.add_argument("--robot")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--persona", default="annie", help="情景模式用的性格")
    parser.add_argument("--repeat", type=int, default=1, help="情景模式每个情景跑几次")
    parser.add_argument("--judge", default="qwen3.8-max-0902", help="评审模型")
    parser.add_argument("--control", action="store_true", help="回放模式：两边用同一提示词，量随机噪声")
    parser.add_argument("--database", type=Path, default=ROOT / "var/runtime.db")
    args = parser.parse_args()
    if not 1 <= args.limit <= 300 or not 1 <= args.repeat <= 5:
        parser.error("--limit 须在 1–300、--repeat 须在 1–5 之间")
    providers = setup()
    {"摘要": mode_summary, "心情": mode_mood, "动作": mode_motion, "反思": mode_reflect,
     "情景": mode_scenes, "回放": mode_replay}[args.mode](args, providers)


if __name__ == "__main__":
    main()
