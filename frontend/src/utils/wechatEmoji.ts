/**
 * Comprehensive WeChat emoji text-code → Unicode emoji mapping.
 *
 * WeChat encodes inline emojis as [Name] in message text.
 * Names can be Chinese (e.g. [微笑]) or English (e.g. [Smile]).
 * This map covers the full built-in emoji set across WeChat versions.
 *
 * Key: the text inside brackets (case-sensitive for English).
 * Value: corresponding Unicode emoji character(s).
 */

const WECHAT_EMOJI_MAP: Record<string, string> = {
  // ──────────────── Classic face emojis ────────────────
  "微笑":     "😊",
  "Smile":    "😊",
  "撇嘴":     "😏",
  "Grimace":  "😏",
  "色":       "😍",
  "Drool":    "😍",
  "发呆":     "😳",
  "Scowl":    "😳",
  "得意":     "😎",
  "CoolGuy":  "😎",
  "流泪":     "😢",
  "Sob":      "😢",
  "害羞":     "☺️",
  "Shy":      "☺️",
  "闭嘴":     "🤐",
  "Shutup":   "🤐",
  "睡":       "😴",
  "Sleep":    "😴",
  "大哭":     "😭",
  "Cry":      "😭",
  "尴尬":     "😅",
  "Awkward":  "😅",
  "发怒":     "😡",
  "Angry":    "😡",
  "调皮":     "😜",
  "Tongue":   "😜",
  "呲牙":     "😁",
  "Grin":     "😁",
  "惊讶":     "😲",
  "Surprise": "😲",
  "难过":     "😞",
  "Frown":    "😞",
  "抓狂":     "😫",
  "Scream":   "😫",
  "吐":       "🤮",
  "Puke":     "🤮",
  "偷笑":     "🤭",
  "Chuckle":  "🤭",
  "可爱":     "🥰",
  "Joyful":   "🥰",
  "白眼":     "🙄",
  "Slight":   "🙄",
  "傲慢":     "😤",
  "Smug":     "😤",
  "饥饿":     "😋",
  "Hungry":   "😋",
  "困":       "😪",
  "Drowsy":   "😪",
  "惊恐":     "😨",
  "Panic":    "😨",
  "流汗":     "😓",
  "Sweat":    "😓",
  "憨笑":     "😄",
  "Laugh":    "😄",
  "大兵":     "💂",
  "Commando": "💂",
  "奋斗":     "💪",
  "Determined": "💪",
  "咒骂":     "🤬",
  "Curse":    "🤬",
  "疑问":     "😕",
  "Confused": "😕",
  "嘘":       "🤫",
  "Shhh":     "🤫",
  "晕":       "😵",
  "Dizzy":    "😵",
  "折磨":     "😩",
  "Tormented": "😩",
  "衰":       "💀",
  "Skull":    "💀",
  "骷髅":     "💀",

  // ──────────────── Gesture / action emojis ────────────────
  "敲打":     "🔨",
  "Hammer":   "🔨",
  "再见":     "👋",
  "Wave":     "👋",
  "擦汗":     "😥",
  "Relieved": "😥",
  "抠鼻":     "🤏",
  "PickNose": "🤏",
  "鼓掌":     "👏",
  "Clap":     "👏",
  "糗大了":   "😳",
  "Shame":    "😳",
  "坏笑":     "😈",
  "Trick":    "😈",
  "左哼哼":   "😤",
  "Bah！L":   "😤",
  "右哼哼":   "😤",
  "Bah！R":   "😤",
  "哈欠":     "🥱",
  "Yawn":     "🥱",
  "鄙视":     "😒",
  "Lookdown": "😒",
  "委屈":     "😣",
  "Wronged":  "😣",
  "快哭了":   "🥺",
  "Puling":   "🥺",
  "阴险":     "😈",
  "Sly":      "😈",
  "亲亲":     "😘",
  "Kiss":     "😘",
  "吓":       "😱",
  "Scared":   "😱",
  "可怜":     "🥺",
  "Poor":     "🥺",

  // ──────────────── Hand gestures ────────────────
  "强":       "👍",
  "ThumbsUp": "👍",
  "弱":       "👎",
  "ThumbsDown": "👎",
  "握手":     "🤝",
  "Shake":    "🤝",
  "胜利":     "✌️",
  "Victory":  "✌️",
  "抱拳":     "🙏",
  "Salute":   "🙏",
  "勾引":     "👆",
  "Beckon":   "👆",
  "拳头":     "✊",
  "Fist":     "✊",
  "差劲":     "👎",
  "Pinky":    "👎",
  "爱你":     "🤟",
  "RockOn":   "🤟",
  "NO":       "🙅",
  "No":       "🙅",
  "OK":       "👌",
  "Ok":       "👌",
  "合十":     "🙏",
  "Worship":  "🙏",
  "比心":     "🫰",

  // ──────────────── Hearts / love ────────────────
  "爱情":     "💕",
  "InLove":   "💕",
  "爱心":     "❤️",
  "Heart":    "❤️",
  "心碎":     "💔",
  "BrokenHeart": "💔",
  "飞吻":     "😘",
  "Blowkiss": "😘",
  "示爱":     "💋",
  "Lips":     "💋",
  "嘴唇":     "👄",

  // ──────────────── Animated / action (mapped to closest static emoji) ────────────────
  "跳跳":     "🤸",
  "Waddle":   "🤸",
  "发抖":     "😰",
  "Tremble":  "😰",
  "怄火":     "😡",
  "Aaagh!":   "😡",
  "转圈":     "💫",
  "Twirl":    "💫",
  "磕头":     "🙇",
  "Kotow":    "🙇",
  "回头":     "🔙",
  "Dramatic": "🔙",
  "跳绳":     "🏃",
  "JumpRope": "🏃",
  "挥手":     "👋",
  "激动":     "🤩",
  "Excited":  "🤩",
  "街舞":     "🕺",
  "Hooray":   "🕺",
  "献吻":     "💋",
  "Smooch":   "💋",
  "左太极":   "☯️",
  "TaiChi L": "☯️",
  "右太极":   "☯️",
  "TaiChi R": "☯️",

  // ──────────────── Food & drink ────────────────
  "菜刀":     "🔪",
  "Cleaver":  "🔪",
  "西瓜":     "🍉",
  "Watermelon": "🍉",
  "啤酒":     "🍺",
  "Beer":     "🍺",
  "咖啡":     "☕",
  "Coffee":   "☕",
  "饭":       "🍚",
  "Rice":     "🍚",
  "蛋糕":     "🎂",
  "Cake":     "🎂",
  "棒棒糖":   "🍭",
  "Lollipop": "🍭",
  "喝奶":     "🍼",
  "Milk":     "🍼",
  "下面":     "🍜",
  "Noodles":  "🍜",
  "香蕉":     "🍌",
  "Banana":   "🍌",
  "茶":       "🍵",
  "Tea":      "🍵",
  "吃瓜":     "🍿",
  "Popcorn":  "🍿",

  // ──────────────── Animals ────────────────
  "猪头":     "🐷",
  "Pig":      "🐷",
  "瓢虫":     "🐞",
  "Ladybug":  "🐞",
  "熊猫":     "🐼",
  "Panda":    "🐼",
  "青蛙":     "🐸",
  "Frog":     "🐸",
  "旺柴":     "🐶",
  "Doge":     "🐶",

  // ──────────────── Nature / weather ────────────────
  "玫瑰":     "🌹",
  "Rose":     "🌹",
  "凋谢":     "🥀",
  "Wilt":     "🥀",
  "月亮":     "🌙",
  "Moon":     "🌙",
  "太阳":     "☀️",
  "Sun":      "☀️",
  "多云":     "⛅",
  "Cloudy":   "⛅",
  "下雨":     "🌧️",
  "Rain":     "🌧️",
  "闪电":     "⚡",
  "Lightning": "⚡",
  "花朵":     "🌸",
  "Flower":   "🌸",

  // ──────────────── Objects ────────────────
  "炸弹":     "💣",
  "Bomb":     "💣",
  "刀":       "🗡️",
  "Dagger":   "🗡️",
  "便便":     "💩",
  "Poop":     "💩",
  "礼物":     "🎁",
  "Gift":     "🎁",
  "灯泡":     "💡",
  "Lightbulb": "💡",
  "闹钟":     "⏰",
  "Alarm":    "⏰",
  "雨伞":     "☂️",
  "Umbrella": "☂️",
  "彩球":     "🎈",
  "Balloon":  "🎈",
  "钻戒":     "💍",
  "Ring":     "💍",
  "沙发":     "🛋️",
  "Sofa":     "🛋️",
  "纸巾":     "🧻",
  "Tissue":   "🧻",
  "药":       "💊",
  "Pills":    "💊",
  "手枪":     "🔫",
  "Gun":      "🔫",
  "钞票":     "💵",
  "Dollars":  "💵",
  "邮件":     "📧",
  "Email":    "📧",
  "风车":     "🎡",
  "Windmill": "🎡",
  "灯笼":     "🏮",
  "Lantern":  "🏮",
  "鞭炮":     "🧨",
  "Firecracker": "🧨",

  // ──────────────── Sports / transport ────────────────
  "篮球":     "🏀",
  "Basketball": "🏀",
  "乒乓":     "🏓",
  "PingPong": "🏓",
  "足球":     "⚽",
  "Soccer":   "⚽",
  "飞机":     "✈️",
  "Airplane": "✈️",
  "开车":     "🚗",
  "Car":      "🚗",
  "高铁左车头": "🚄",
  "TrainL":   "🚄",
  "车厢":     "🚃",
  "TrainBody": "🚃",
  "高铁右车头": "🚄",
  "TrainR":   "🚄",

  // ──────────────── Special / festive ────────────────
  "双喜":     "囍",
  "Happy":    "囍",
  "发财":     "🤑",
  "Rich":     "🤑",
  "K歌":      "🎤",
  "Singing":  "🎤",
  "购物":     "🛍️",
  "Shopping": "🛍️",
  "帅":       "😎",
  "Handsome": "😎",
  "喝彩":     "👏",
  "Applaud":  "👏",
  "祈祷":     "🙏",
  "Pray":     "🙏",
  "爆筋":     "💢",
  "Lash Out": "💢",
  "福":       "🧧",
  "Fortune":  "🧧",
  "烟花":     "🎆",
  "Fireworks": "🎆",
  "庆祝":     "🎉",
  "Party":    "🎉",
  "Celebrate": "🎉",

  // ──────────────── Newer WeChat emojis (added ~2019-2024) ────────────────
  "拥抱":     "🤗",
  "Hug":      "🤗",
  "眨眼睛":   "😉",
  "Blink":    "😉",
  "泪奔":     "😭",
  "CryingFace": "😭",
  "无奈":     "😑",
  "Speechless": "😑",
  "卖萌":     "🥰",
  "Cute":     "🥰",
  "小纠结":   "😖",
  "SmallTangle": "😖",
  "加油":     "💪",
  "Cheer":    "💪",
  "汗":       "😓",
  "天啊":     "😱",
  "OMG":      "😱",
  "社会社会": "🤔",
  "Emm":      "🤔",
  "好的":     "👌",
  "NoProb":   "👌",
  "打脸":     "🤦",
  "FacePalm": "🤦",
  "加油加油": "💪",
  "GoForIt":  "💪",
  "哇":       "😮",
  "Wow":      "😮",
  "翻白眼":   "🙄",
  "Boring":   "🙄",
  "666":      "🤙",
  "让我看看": "👀",
  "LetMeSee": "👀",
  "叹气":     "😮‍💨",
  "Sigh":     "😮‍💨",
  "苦涩":     "😣",
  "Bitter":   "😣",
  "裂开":     "😱",
  "Crack":    "😱",
  "捂脸":     "🤦",
  "Facepalm": "🤦",
  "奸笑":     "😏",
  "Smirk":    "😏",
  "机智":     "🧐",
  "Smart":    "🧐",
  "皱眉":     "😟",
  "Worried":  "😟",
  "耶":       "✌️",
  "Yeah!":    "✌️",
  "吃糖":     "🍬",
  "Candy":    "🍬",
  "生病":     "🤒",
  "Sick":     "🤒",
  "破涕为笑": "😂",
  "HappyCry": "😂",
  "恐惧":     "😨",
  "Terror":   "😨",
  "脑阔疼":   "🤯",
  "Headache": "🤯",
  "沧桑":     "😩",
  "Weary":    "😩",
  "嗑":       "🌰",
  "Crunch":   "🌰",
  "Respect":  "🫡",
  "吃瓜群众": "🍿",
  "Onlooker": "🍿",
  "嘿哈":     "🤪",
  "HeyHey":   "🤪",

  // ──────────────── Additional common aliases ────────────────
  "笑脸":     "😊",
  "哭":       "😢",
  "心":       "❤️",
  "赞":       "👍",
  "踩":       "👎",
  "鲜花":     "🌹",
  "星星":     "⭐",
  "Star":     "⭐",
  "闪光":     "✨",
  "Sparkles": "✨",
};

// Build a regex that matches [emojiName] for all known names (sorted longest-first to avoid partial matches)
const sortedKeys = Object.keys(WECHAT_EMOJI_MAP).sort((a, b) => b.length - a.length);
const escapedKeys = sortedKeys.map((k) => k.replace(/[.*+?^${}()|[\]\\!/]/g, "\\$&"));
const EMOJI_REGEX = new RegExp(`\\[(${escapedKeys.join("|")})\\]`, "g");

/**
 * Replace all WeChat emoji text codes in a string with Unicode emoji.
 * e.g. "你好[微笑]" → "你好😊"
 *      "Hello [Hammer]" → "Hello 🔨"
 */
export function replaceWechatEmojis(text: string): string {
  if (!text) return text;
  return text.replace(EMOJI_REGEX, (_, name) => WECHAT_EMOJI_MAP[name] || `[${name}]`);
}

/**
 * For React rendering: splits text into alternating string/emoji segments
 * so emoji can be wrapped in <span> elements for optional custom styling.
 * Returns an array of { type: "text" | "emoji", value: string } segments.
 */
export interface EmojiSegment {
  type: "text" | "emoji";
  value: string;
  key: string;     // unique key for React rendering
}

export function parseWechatEmojis(text: string): EmojiSegment[] {
  if (!text) return [{ type: "text", value: "", key: "t0" }];

  const segments: EmojiSegment[] = [];
  let lastIndex = 0;
  let matchIndex = 0;

  // Reset regex state
  EMOJI_REGEX.lastIndex = 0;

  let match: RegExpExecArray | null;
  while ((match = EMOJI_REGEX.exec(text)) !== null) {
    // Push preceding text
    if (match.index > lastIndex) {
      segments.push({
        type: "text",
        value: text.slice(lastIndex, match.index),
        key: `t${matchIndex}`,
      });
    }
    // Push emoji
    const name = match[1];
    segments.push({
      type: "emoji",
      value: WECHAT_EMOJI_MAP[name] || `[${name}]`,
      key: `e${matchIndex}`,
    });
    lastIndex = match.index + match[0].length;
    matchIndex++;
  }

  // Push trailing text
  if (lastIndex < text.length) {
    segments.push({
      type: "text",
      value: text.slice(lastIndex),
      key: `t${matchIndex}`,
    });
  }

  if (segments.length === 0) {
    segments.push({ type: "text", value: text, key: "t0" });
  }

  return segments;
}

export { WECHAT_EMOJI_MAP };
