"""zh→ja 方向のターゲット側（日本語）チェック。qa_translation.py から使う。

_lib.find_non_jis_han と同じ基準（CP932 外＋言語 pack の NON_JIS_HAN）。
日本語訳に残った簡体字・原文の誤字・未登録の固有名詞を検出する。

拡張: P0-3 で Finding / ResidueConfig / find_zh_vocab / find_cn_residue_toml /
find_term_src_leftover を追加。

拡張: P2-2 / P2-3（stage11）で age_policy の文型4分類（find_age_mentions）と
age_policy_report の TOML 記録（build_age_policy_records /
write_age_policy_report）、残渣の語単位判定（simp_words / ja_ok_words）を追加。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
import tomllib
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _lib import atomic_write_text, dump_toml, find_non_jis_han, split_paras  # noqa: E402

__all__ = [
    "Finding",
    "ResidueConfig",
    "AGE_CLASSES",
    "AGE_REPORT_MD",
    "AGE_REPORT_TOML",
    "AgeFinding",
    "build_age_policy_records",
    "find_age_mentions",
    "find_non_jis_han",
    "find_zh_vocab",
    "find_cn_residue_toml",
    "find_cn_residue_fields",
    "find_term_src_leftover",
    "render_age_policy_md",
    "render_age_policy_toml",
    "write_age_policy_report",
]

# ── 内部データ ──────────────────────────────────────────────────────────

_DEF_DATA: pathlib.Path = (pathlib.Path(__file__).resolve().parent /
                           "data" / "zh2ja_residue.toml")


def _load_def_toml() -> dict[str, dict]:
    """組み込み辞書 TOML を読む。無ければ空 dict。"""
    try:
        with open(_DEF_DATA, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _load_vocab(cfg: ResidueConfig | None) -> dict[str, str]:
    d = _load_def_toml()
    base: dict[str, str] = {}
    base.update(d.get("vocab", {}))
    if cfg is not None:
        base.update(cfg.vocab)
    return base


def _load_idioms(cfg: ResidueConfig | None) -> dict[str, str]:
    d = _load_def_toml()
    base: dict[str, str] = {}
    base.update(d.get("idioms", {}))
    if cfg is not None:
        base.update(cfg.idioms)
    return base


def _load_cn_words() -> list[str]:
    d = _load_def_toml()
    return list(d.get("cn_words", {}))


def _load_allow(cfg: ResidueConfig | None) -> set[str]:
    d = _load_def_toml()
    base: set[str] = set(d.get("allow", {}))
    if cfg is not None:
        base |= cfg.allow
    return base


def _load_simp(cfg: ResidueConfig | None) -> dict[str, str]:
    """簡体字専用文字（単字）。cfg の追加分を上書き合成する。"""
    d = _load_def_toml()
    base: dict[str, str] = dict(d.get("simp", {}))
    if cfg is not None:
        base.update(cfg.simp)
    return base


def _load_simp_words(cfg: ResidueConfig | None) -> dict[str, str]:
    """2字以上の語（語単位の判定を主とする）。cfg の追加分を上書き合成する。"""
    d = _load_def_toml()
    base: dict[str, str] = dict(d.get("simp_words", {}))
    if cfg is not None:
        base.update(cfg.simp_words)
    return base


def _load_ja_ok_words(cfg: ResidueConfig | None) -> set[str]:
    """日本語として正当な語（単字ヒットの除外根拠）。"""
    d = _load_def_toml()
    base: set[str] = set(d.get("ja_ok_words", {}))
    if cfg is not None:
        base |= cfg.ja_ok_words
    return base


#: 常用漢字（2010年内閣告示「常用漢字表」・2136字）の一覧。
#: 単字ヒットの「非常用字」判定に使う。常用漢字は日本語で使う字なので、
#: 単字では検出しない（語単位の辞書 simp_words / ja_ok_words に委ねる）。
_JOYO_KANJI = (
    "亜哀挨愛曖悪握圧扱宛嵐安案暗以衣位囲医依委威為畏胃尉異移萎偉椅彙意違維慰遺緯域育一壱逸茨芋引印因"
    "咽姻員院淫陰飲隠韻右宇羽雨唄鬱畝浦運雲永泳英映栄営詠影鋭衛易疫益液駅悦越謁閲円延沿炎怨宴媛援園煙"
    "猿遠鉛塩演縁艶汚王凹央応往押旺欧殴桜翁奥横岡屋億憶臆虞乙俺卸音恩温穏下化火加可仮何花佳価果河苛科"
    "架夏家荷華菓貨渦過嫁暇禍靴寡歌箇稼課蚊牙瓦我画芽賀雅餓介回灰会快戒改怪拐悔海界皆械絵開階塊楷解潰"
    "壊懐諧貝外劾害崖涯街慨蓋該概骸垣柿各角拡革格核殻郭覚較隔閣確獲嚇穫学岳楽額顎掛潟括活喝渇割葛滑褐"
    "轄且株釜鎌刈干刊甘汗缶完肝官冠巻看陥乾勘患貫寒喚堪換敢棺款間閑勧寛幹感漢慣管関歓監緩憾還館環簡観"
    "韓艦鑑丸含岸岩玩眼頑顔願企伎危机気岐希忌汽奇祈季紀軌既記起飢鬼帰基寄規亀喜幾揮期棋貴棄毀旗器畿輝"
    "機騎技宜偽欺義疑儀戯擬犠議菊吉喫詰却客脚逆虐九久及弓丘旧休吸朽臼求究泣急級糾宮救球給嗅窮牛去巨居"
    "拒拠挙虚許距魚御漁凶共叫狂京享供協況峡挟狭恐恭胸脅強教郷境橋矯鏡競響驚仰暁業凝曲局極玉巾斤均近金"
    "菌勤琴筋僅禁緊錦謹襟吟銀区句苦駆具惧愚空偶遇隅串屈掘窟熊繰君訓勲薫軍郡群兄刑形系径茎係型契計恵啓"
    "掲渓経蛍敬景軽傾携継詣慶憬稽憩警鶏芸迎鯨隙劇撃激桁欠穴血決結傑潔月犬件見券肩建研県倹兼剣拳軒健険"
    "圏堅検嫌献絹遣権憲賢謙鍵繭顕験懸元幻玄言弦限原現舷減源厳己戸古呼固股虎孤弧故枯個庫湖雇誇鼓錮顧五"
    "互午呉後娯悟碁語誤護口工公勾孔功巧広甲交光向后好江考行坑孝抗攻更効幸拘肯侯厚恒洪皇紅荒郊香候校耕"
    "航貢降高康控梗黄喉慌港硬絞項溝鉱構綱酵稿興衡鋼講購乞号合拷剛傲豪克告谷刻国黒穀酷獄骨駒込頃今困昆"
    "恨根婚混痕紺魂墾懇左佐沙査砂唆差詐鎖座挫才再災妻采砕宰栽彩採済祭斎細菜最裁債催塞歳載際埼在材剤財"
    "罪崎作削昨柵索策酢搾錯咲冊札刷刹拶殺察撮擦雑皿三山参桟蚕惨産傘散算酸賛残斬暫士子支止氏仕史司四市"
    "矢旨死糸至伺志私使刺始姉枝祉肢姿思指施師恣紙脂視紫詞歯嗣試詩資飼誌雌摯賜諮示字寺次耳自似児事侍治"
    "持時滋慈辞磁餌璽鹿式識軸七叱失室疾執湿嫉漆質実芝写社車舎者射捨赦斜煮遮謝邪蛇尺借酌釈爵若弱寂手主"
    "守朱取狩首殊珠酒腫種趣寿受呪授需儒樹収囚州舟秀周宗拾秋臭修袖終羞習週就衆集愁酬醜蹴襲十汁充住柔重"
    "従渋銃獣縦叔祝宿淑粛縮塾熟出述術俊春瞬旬巡盾准殉純循順準潤遵処初所書庶暑署緒諸女如助序叙徐除小升"
    "少召匠床抄肖尚招承昇松沼昭宵将消症祥称笑唱商渉章紹訟勝掌晶焼焦硝粧詔証象傷奨照詳彰障憧衝賞償礁鐘"
    "上丈冗条状乗城浄剰常情場畳蒸縄壌嬢錠譲醸色拭食植殖飾触嘱織職辱尻心申伸臣芯身辛侵信津神唇娠振浸真"
    "針深紳進森診寝慎新審震薪親人刃仁尽迅甚陣尋腎須図水吹垂炊帥粋衰推酔遂睡穂随髄枢崇数据杉裾寸瀬是井"
    "世正生成西声制姓征性青斉政星牲省凄逝清盛婿晴勢聖誠精製誓静請整醒税夕斥石赤昔析席脊隻惜戚責跡積績"
    "籍切折拙窃接設雪摂節説舌絶千川仙占先宣専泉浅洗染扇栓旋船戦煎羨腺詮践箋銭潜線遷選薦繊鮮全前善然禅"
    "漸膳繕狙阻祖租素措粗組疎訴塑遡礎双壮早争走奏相荘草送倉捜挿桑巣掃曹曽爽窓創喪痩葬装僧想層総遭槽踪"
    "操燥霜騒藻造像増憎蔵贈臓即束足促則息捉速側測俗族属賊続卒率存村孫尊損遜他多汰打妥唾堕惰駄太対体耐"
    "待怠胎退帯泰堆袋逮替貸隊滞態戴大代台第題滝宅択沢卓拓託濯諾濁但達脱奪棚誰丹旦担単炭胆探淡短嘆端綻"
    "誕鍛団男段断弾暖談壇地池知値恥致遅痴稚置緻竹畜逐蓄築秩窒茶着嫡中仲虫沖宙忠抽注昼柱衷酎鋳駐著貯丁"
    "弔庁兆町長挑帳張彫眺釣頂鳥朝貼超腸跳徴嘲潮澄調聴懲直勅捗沈珍朕陳賃鎮追椎墜通痛塚漬坪爪鶴低呈廷弟"
    "定底抵邸亭貞帝訂庭逓停偵堤提程艇締諦泥的笛摘滴適敵溺迭哲鉄徹撤天典店点展添転填田伝殿電斗吐妬徒途"
    "都渡塗賭土奴努度怒刀冬灯当投豆東到逃倒凍唐島桃討透党悼盗陶塔搭棟湯痘登答等筒統稲踏糖頭謄藤闘騰同"
    "洞胴動堂童道働銅導瞳峠匿特得督徳篤毒独読栃凸突届屯豚頓貪鈍曇丼那奈内梨謎鍋南軟難二尼弐匂肉虹日入"
    "乳尿任妊忍認寧熱年念捻粘燃悩納能脳農濃把波派破覇馬婆罵拝杯背肺俳配排敗廃輩売倍梅培陪媒買賠白伯拍"
    "泊迫剥舶博薄麦漠縛爆箱箸畑肌八鉢発髪伐抜罰閥反半氾犯帆汎伴判坂阪板版班畔般販斑飯搬煩頒範繁藩晩番"
    "蛮盤比皮妃否批彼披肥非卑飛疲秘被悲扉費碑罷避尾眉美備微鼻膝肘匹必泌筆姫百氷表俵票評漂標苗秒病描猫"
    "品浜貧賓頻敏瓶不夫父付布扶府怖阜附訃負赴浮婦符富普腐敷膚賦譜侮武部舞封風伏服副幅復福腹複覆払沸仏"
    "物粉紛雰噴墳憤奮分文聞丙平兵併並柄陛閉塀幣弊蔽餅米壁璧癖別蔑片辺返変偏遍編弁便勉歩保哺捕補舗母募"
    "墓慕暮簿方包芳邦奉宝抱放法泡胞俸倣峰砲崩訪報蜂豊飽褒縫亡乏忙坊妨忘防房肪某冒剖紡望傍帽棒貿貌暴膨"
    "謀頬北木朴牧睦僕墨撲没勃堀本奔翻凡盆麻摩磨魔毎妹枚昧埋幕膜枕又末抹万満慢漫未味魅岬密蜜脈妙民眠矛"
    "務無夢霧娘名命明迷冥盟銘鳴滅免面綿麺茂模毛妄盲耗猛網目黙門紋問冶夜野弥厄役約訳薬躍闇由油喩愉諭輸"
    "癒唯友有勇幽悠郵湧猶裕遊雄誘憂融優与予余誉預幼用羊妖洋要容庸揚揺葉陽溶腰様瘍踊窯養擁謡曜抑沃浴欲"
    "翌翼拉裸羅来雷頼絡落酪辣乱卵覧濫藍欄吏利里理痢裏履璃離陸立律慄略柳流留竜粒隆硫侶旅虜慮了両良料涼"
    "猟陵量僚領寮療瞭糧力緑林厘倫輪隣臨瑠涙累塁類令礼冷励戻例鈴零霊隷齢麗暦歴列劣烈裂恋連廉練錬呂炉賂"
    "路露老労弄郎朗浪廊楼漏籠六録麓論和話賄脇惑枠湾腕"
)
_JOYO_SET = frozenset(_JOYO_KANJI)


def _is_jp_common(ch: str) -> bool:
    """常用漢字（日本語で使う字）かどうか。"""
    return ch in _JOYO_SET


def _word_windows(s: str, idx: int) -> list[str]:
    """位置 idx の字を周辺と結合した2字の語（直前1字・直後1字の窓）。"""
    w: list[str] = []
    if idx > 0:
        w.append(s[idx - 1:idx + 1])
    if idx + 1 < len(s):
        w.append(s[idx:idx + 2])
    return w


def _excluded_by_word_dict(s: str, idx: int, ja_ok_words: set[str],
                           simp_words: dict[str, str]) -> bool:
    """周辺と結合した語が語辞書（正当語 / 2字語）にあれば単字ヒットを除外する。"""
    for w in _word_windows(s, idx):
        if w in ja_ok_words or w in simp_words:
            return True
    return False


def _find_single_simp_hit(probe: str, ch: str, ja_ok_words: set[str],
                          simp_words: dict[str, str]) -> int | None:
    """単字 ch の最初の「語辞書で除外されない」出現位置。無ければ None。"""
    start = 0
    while True:
        idx = probe.find(ch, start)
        if idx < 0:
            return None
        if not _excluded_by_word_dict(probe, idx, ja_ok_words, simp_words):
            return idx
        start = idx + 1


# ── データ構造 ──────────────────────────────────────────────────────────

@dataclass
class Finding:
    """1件の検出結果。rule は検出種別、value は検出された語、context は
    前後20字、line は行番号（無ければ0）、safe_fix は文脈非依存で安全な
    置換先（無ければ None）。"""
    rule: str
    value: str
    context: str
    line: int = 0
    safe_fix: str | None = None


@dataclass
class ResidueConfig:
    """検出設定。known_terms: 意図的に簡体字を含む既定訳（除外）。
    allow: 誤検知許容ワード。vocab/idioms: 追加の中國語→日本語対応表。
    simp: 追加の簡体字専用文字（単字）。
    simp_words: 追加の2字以上の語（語単位の判定）。
    ja_ok_words: 追加の正当語（単字ヒットの除外根拠）。"""
    known_terms: set[str] = field(default_factory=set)
    allow: set[str] = field(default_factory=set)
    vocab: dict[str, str] = field(default_factory=dict)
    idioms: dict[str, str] = field(default_factory=dict)
    simp: dict[str, str] = field(default_factory=dict)
    simp_words: dict[str, str] = field(default_factory=dict)
    ja_ok_words: set[str] = field(default_factory=set)


# ── 検出関数 ────────────────────────────────────────────────────────────

def _context(s: str, pos: int, width: int = 20) -> str:
    """文字列 s の位置 pos（検出語の先頭）の前後 width 字を返す。"""
    start = max(0, pos - width)
    end = min(len(s), pos + width)
    ctx = s[start:end]
    if start > 0:
        ctx = "…" + ctx
    if end < len(s):
        ctx = ctx + "…"
    return ctx


def find_zh_vocab(text: str, config: ResidueConfig | None = None) -> list[Finding]:
    """中国語語彙・成語・簡体字の残渣を検出する。

    - vocab / idiom: 語単位（字は日本語正字でも語が中国語）の検出
    - simp_words: 2字以上の簡体字語（語単位の判定を主とする）
    - simp: 単字。周辺と結合した語が語辞書（ja_ok_words / simp_words）に無く、
      かつ非常用字（常用漢字外）のときだけ検出する
      （例: 馥郁 の 郁 は語 馥郁 が ja_ok_words にあるため除外）
    """
    findings: list[Finding] = []
    vocab = _load_vocab(config)
    idioms = _load_idioms(config)
    simp = _load_simp(config)
    simp_words = _load_simp_words(config)
    ja_ok_words = _load_ja_ok_words(config)
    allow = _load_allow(config)
    known = set(config.known_terms) if config is not None else set()
    src_re = re.compile(r"(?:name_src|term_src|target_src|title_src)\s*=")

    lines = text.split("\n")
    for lineno, line in enumerate(lines, 1):
        if src_re.search(line):
            continue
        if line.lstrip().startswith(">"):  # 対訳原文ブロック
            continue

        probe = line
        # known_terms を除去
        for t in known:
            probe = probe.replace(t, "")

        allow_clean = set()
        for a in allow:
            allow_clean.add(a)
        for a in allow_clean:
            if a in probe:
                probe = probe.replace(a, "")

        # VOCAB
        for kw, rep in vocab.items():
            idx = probe.find(kw)
            if idx >= 0:
                findings.append(Finding(
                    rule="vocab",
                    value=kw,
                    context=_context(probe, idx),
                    line=lineno,
                    safe_fix=rep,
                ))

        # IDIOMS
        for kw, rep in idioms.items():
            idx = probe.find(kw)
            if idx >= 0:
                findings.append(Finding(
                    rule="idiom",
                    value=kw,
                    context=_context(probe, idx),
                    line=lineno,
                    safe_fix=rep,
                ))

        # SIMP_WORDS — 2字以上の語（語単位の判定を主とする）
        for kw, rep in simp_words.items():
            idx = probe.find(kw)
            if idx >= 0:
                findings.append(Finding(
                    rule="simp_word",
                    value=kw,
                    context=_context(probe, idx),
                    line=lineno,
                    safe_fix=rep,
                ))

        # SIMP — 単字。周辺と結合した語が語辞書（ja_ok_words / simp_words）に
        # 無く、かつ非常用字（常用漢字外）のときだけ検出する。
        for ch, rep in simp.items():
            if _is_jp_common(ch):
                continue
            idx = _find_single_simp_hit(probe, ch, ja_ok_words, simp_words)
            if idx is not None:
                findings.append(Finding(
                    rule="simp",
                    value=ch,
                    context=_context(probe, idx),
                    line=lineno,
                    safe_fix=rep,
                ))

    return findings


def find_cn_residue_toml(toml_path: pathlib.Path,
                         config: ResidueConfig | None = None) -> list[Finding]:
    """分析TOMLの日本語フィールドに残った中国語を検出する。

    *_src キーの値・コメント行・age_policy_report は除外。
    検出規則 R1 (CP932外CJK), R2 (機能語), R3 (定型語),
    R4 (全角読点), R5 (人称/呼称)。
    """
    findings: list[Finding] = []
    toml_path = pathlib.Path(toml_path)

    if "age_policy_report" in toml_path.name:
        return findings

    cn_words = _load_cn_words()
    cn_mark = set("你我她她们这吗呢哦呀嘛怎什为从对给还让说您")
    allow = _load_allow(config)

    keyval_re = re.compile(
        r'\b(?:name_src|term_src|target_src|title_src|src)\s*=\s*"(?:[^"\\]|\\.)*"'
    )
    person_re = re.compile(r'(?:first_person|second_person)\s*=\s*"[^"]*[你我您]')
    form_re = re.compile(r'form\s*=\s*"[^"]*[你我您]')

    try:
        text = toml_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return findings

    for lineno, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("#"):
            continue

        probe = keyval_re.sub("", line)

        # allow 除去
        for a in allow:
            probe = probe.replace(a, "")

        reasons: list[str] = []

        # R1: CP932 外の CJK
        for c in probe:
            if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf":
                try:
                    c.encode("cp932")
                except UnicodeEncodeError:
                    reasons.append(f"非JIS字:{c}")

        # R3: 定型語
        for w in cn_words:
            if w in probe:
                reasons.append(f"中文語:{w}")

        # R4: 全角読点
        if "，" in probe:
            reasons.append("中文読点")

        # R2: 機能語
        cnt = sum(1 for c in probe if c in cn_mark)
        has_kana = bool(re.search(r"[ぁ-んァ-ヴー]", probe))
        if cnt >= 2 and not has_kana:
            reasons.append(f"中文機能語x{cnt}")

        # R5: 人称/呼称
        if person_re.search(probe) or form_re.search(probe):
            reasons.append("人称/呼称に中文")

        if reasons:
            findings.append(Finding(
                rule="cn_field",
                value=" / ".join(reasons),
                context=line.strip()[:80],
                line=lineno,
            ))

    return findings


def find_cn_residue_fields(data, config: ResidueConfig | None = None) -> list[Finding]:
    """パース済み TOML データの「対象言語の欄」に残った中国語を検出する。

    キーが ``src`` / ``*_src`` の値は原文側として除外し、dict / list を再帰して
    文字列値だけを検査する（行の正規表現では ``*_src`` の全形を除外できない）。
    検出規則は find_cn_residue_toml と同じ:

    - R1: CP932 外の CJK（非JIS字）
    - R2: 中国語の機能語（かな無しで2字以上）
    - R3: 定型語（cn_words。既定は空）
    - R4: 全角読点（，）
    - R5: 人称/呼称の欄（first_person / second_person / form）に中国語の人称

    空のデータ（記録なし・コメントのみの TOML）は検出なし（「未分析」扱いと矛盾しない）。
    """
    findings: list[Finding] = []
    cn_words = _load_cn_words()
    cn_mark = set("你我她她们这吗呢哦呀嘛怎什为从对给还让说您")
    allow = _load_allow(config)
    kana_re = re.compile(r"[ぁ-んァ-ヴー]")

    def _is_src_key(key: str) -> bool:
        # aliases は別名・愛称・原文の異表記を保持する欄であり、原語形を含む
        # ことが仕様（P0-4）。dismiss 系（別人棄却の相手原文名・理由）も原文名を
        # 保持する仕様のため同列に除外する。原文側として残渣検査から除外する。
        return key == "src" or key.endswith("_src") or key in (
            "aliases", "dismiss_variant", "dismissed_variants", "dismiss_reason")

    def _check(key: str, value: str, path: str) -> None:
        probe = value
        for a in allow:
            probe = probe.replace(a, "")

        reasons: list[str] = []

        # R1: CP932 外の CJK
        for c in probe:
            if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf":
                try:
                    c.encode("cp932")
                except UnicodeEncodeError:
                    reasons.append(f"非JIS字:{c}")

        # R3: 定型語
        for w in cn_words:
            if w in probe:
                reasons.append(f"中文語:{w}")

        # R4: 全角読点
        if "，" in probe:
            reasons.append("中文読点")

        # R2: 機能語
        cnt = sum(1 for c in probe if c in cn_mark)
        if cnt >= 2 and not kana_re.search(probe):
            reasons.append(f"中文機能語x{cnt}")

        # R5: 人称/呼称
        if key in ("first_person", "second_person", "form") and any(
                c in probe for c in "你我您"):
            reasons.append("人称/呼称に中文")

        if reasons:
            findings.append(Finding(
                rule="cn_field",
                value=" / ".join(reasons),
                context=f"{path} = {value[:60]}",
                line=0,
            ))

    def _walk(obj, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k)
                if _is_src_key(key):
                    continue
                child = f"{path}.{key}" if path else key
                if isinstance(v, str):
                    _check(key, v, child)
                else:
                    _walk(v, child)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")
        elif isinstance(obj, str) and path:
            _check("", obj, path)

    _walk(data, "")
    return findings


def find_term_src_leftover(work: pathlib.Path) -> list[Finding]:
    """訳文に terms.toml の term_src が残っていないか検出する。

    terms.toml の term_src 値を収集し、訳文ファイル内にそのまま残っていないか
    スキャンする。
    """
    findings: list[Finding] = []
    work = pathlib.Path(work)

    terms_path = work / "terms.toml"
    if not terms_path.is_file():
        return findings

    # term_src を収集
    try:
        with open(terms_path, "rb") as f:
            terms_data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return findings

    term_src_list: list[str] = []
    entries = terms_data
    if isinstance(entries, dict):
        for row in entries.get("terms", []):
            if isinstance(row, dict):
                src = row.get("term_src", "")
                if src:
                    term_src_list.append(src)

    if not term_src_list:
        return findings

    # 訳文ファイルをスキャン（output 下の .md / .txt）
    output_dirs = list(work.glob("output/*/"))
    for out_dir in output_dirs:
        ch_dirs = [out_dir]
        ch_dirs.extend(sorted(out_dir.glob("ch*")))
        for d in ch_dirs:
            for fpath in sorted(d.glob("*.md")) + sorted(d.glob("*.txt")):
                try:
                    content = fpath.read_text(encoding="utf-8")
                except OSError:
                    continue
                for line_num, line in enumerate(content.split("\n"), 1):
                    for src in term_src_list:
                        if src in line:
                            findings.append(Finding(
                                rule="term_src",
                                value=src,
                                context=line.strip()[:80],
                                line=line_num,
                            ))

    return findings


# ── age_policy（年齢表現の検出・分類・記録。P2-2 / P2-3） ────────────────

#: 年齢表現の文型分類。確認の負担を減らすための分類であり、
#: 「過去の時点だから対象外」のように安全確認から自動で外すものではない。
#: 数値や学校用語の置換で成人になったとは扱わない（成人扱いの可否は別途確認）。
AGE_CLASSES = ("age_diff", "past_time", "other_person", "direct")

#: age_policy_report の記録先（md は TOML からの生成物）
AGE_REPORT_TOML = "age_policy_report-[ネタバレあり].toml"
AGE_REPORT_MD = "age_policy_report-[ネタバレあり].md"

_AGE_DIGITS = "0-9０１２３４５６７８９"
_AGE_NUM = r"(?:[" + _AGE_DIGITS + r"]{1,3}|[一二三四五六七八九十百千零〇两兩]{1,5})"
_AGE_RE = re.compile(
    r"(?<![" + _AGE_DIGITS + r"])(?P<num>" + _AGE_NUM + r")(?P<suf>[岁歲歳])")

#: 文節の区切り（分類の前後文を文節内に限る）
_AGE_BREAK = "，。！？、；：…‥・—―─·「」『』（）()【】〔〕\n\r\t "
_AGE_DIFF_NEAR = re.compile(r"[大小]")
_AGE_DIFF_WORD = re.compile(r"年长|年長|年幼|相差")
_AGE_PAST_AFTER = re.compile(r"^(?:的?[时時]|那|当|當|起)")
_AGE_OTHER_NEAR = re.compile(
    r"他|她|祂|牠|它|其|别人|別人|对方|對方|"
    r"妹妹|弟弟|姐姐|哥哥|儿子|兒子|女儿|女兒|女孩|男孩|少年|少女|孩子")


@dataclass
class AgeFinding:
    """年齢表現の1件。matched は該当語（数＋岁）、age_class は文型分類、
    context は前後20字、line は行番号（1始まり。不明は0）。"""
    matched: str
    age_class: str
    context: str
    line: int = 0


def _age_clause_before(text: str, pos: int, width: int = 8) -> str:
    """pos の直前 width 字から、直近の文節区切りより後ろだけを返す。"""
    pre = text[max(0, pos - width):pos]
    for i in range(len(pre) - 1, -1, -1):
        if pre[i] in _AGE_BREAK:
            return pre[i + 1:]
    return pre


def _age_clause_after(text: str, pos: int, width: int = 4) -> str:
    """pos の直後 width 字から、直近の文節区切りより前だけを返す。"""
    post = text[pos:pos + width]
    for i, ch in enumerate(post):
        if ch in _AGE_BREAK:
            return post[:i]
    return post


def _classify_age_mention(text: str, start: int, end: int) -> str:
    """年齢表現の文型分類（age_diff / past_time / other_person / direct）。

    - age_diff: 年齢差（大X岁 / 比X大Y岁 / 小X岁 等）
    - past_time: 過去の時点（X岁时 / X岁那年 等）
    - other_person: 他人の年齢の言及（三人称・続柄語が近くにある）
    - direct: 上記以外（本人の年齢の直接言及）
    """
    pre = _age_clause_before(text, start)
    post = _age_clause_after(text, end)
    if _AGE_DIFF_NEAR.search(pre[-4:]) or _AGE_DIFF_WORD.search(pre):
        return "age_diff"
    if _AGE_PAST_AFTER.match(post):
        return "past_time"
    if _AGE_OTHER_NEAR.search(pre):
        return "other_person"
    return "direct"


def find_age_mentions(text: str) -> list[AgeFinding]:
    """中国語テキストから年齢表現（数＋岁/歲/歳）を検出し、文型で分類する。

    分類（age_diff / past_time / other_person / direct）は確認の負担を減らす
    ためのもので、**どの分類でも検出リストに残す**（「過去の時点だから対象外」
    にはしない。性的場面との関係は別途確認）。数値や学校用語の置換で成人に
    なったとは扱わない。
    """
    findings: list[AgeFinding] = []
    for lineno, line in enumerate(text.split("\n"), 1):
        for m in _AGE_RE.finditer(line):
            findings.append(AgeFinding(
                matched=m.group(0),
                age_class=_classify_age_mention(line, m.start(), m.end()),
                context=_context(line, m.start()),
                line=lineno,
            ))
    return findings


def _hash_context(context: str) -> str:
    """前後文の sha256 先頭12桁（rulings.text_hash と同じ正規化・桁数）。"""
    norm = context.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def _load_chapters_meta_raw(work: pathlib.Path) -> dict:
    """chapters_meta（TOML/JSON）を読む。無ければ空 dict。"""
    for name in ("chapters_meta.toml", "chapters_meta.json"):
        p = work / name
        if not p.is_file():
            continue
        try:
            if p.suffix == ".json":
                data = json.loads(p.read_text(encoding="utf-8"))
            else:
                with open(p, "rb") as f:
                    data = tomllib.load(f)
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _block_index(meta: dict) -> tuple[dict[str, list[tuple[str, int, int]]],
                                      list[tuple[str, int]]]:
    """chapters_meta から (block 範囲の索引, [(章ID, 段落数)]) を作る。"""
    blocks: dict[str, list[tuple[str, int, int]]] = {}
    chapters: list[tuple[str, int]] = []
    for ch in meta.get("chapters", []) or []:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "")
        try:
            cnt = int(ch.get("n_paras") or 0)
        except (TypeError, ValueError):
            cnt = 0
        chapters.append((cid, cnt))
        bl: list[tuple[str, int, int]] = []
        for b in ch.get("blocks", []) or []:
            if not isinstance(b, dict):
                continue
            rng = b.get("paras") or []
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                try:
                    bl.append((str(b.get("id") or ""), int(rng[0]), int(rng[1])))
                except (TypeError, ValueError):
                    continue
        blocks[cid] = bl
    return blocks, chapters


def _loc_block(blocks: dict[str, list[tuple[str, int, int]]],
               cid: str, n: int) -> str:
    """段落 n が属する block 表記（chNNN/bN）。解決できなければ章IDのみ。"""
    for bid, lo, hi in blocks.get(cid, []):
        if lo <= n <= hi:
            return f"{cid}/{bid}" if bid else cid
    return cid


def _chapter_para(chapters: list[tuple[str, int]], idx: int) -> tuple[str, int]:
    """通し段落番号 idx を (章ID, 章内段落番号) にする。

    chapters_meta が無ければ ("", idx)。範囲外も ("", idx)。
    """
    if not chapters:
        return "", idx
    rest = idx
    for cid, cnt in chapters:
        if cnt <= 0:
            continue
        if rest <= cnt:
            return cid, rest
        rest -= cnt
    return "", idx


def build_age_policy_records(work: pathlib.Path) -> list[dict]:
    """work の原文から age_policy_report の記録（1件ずつ）を組み立てる。

    走査対象: chapters/ch*.paras.toml（無ければ originals/source.md）。
    段落は chapters_meta のブロック範囲で chNNN/bN に対応づける
    （解決できないときは章IDのみ。章も不明なら空）。
    1件 = {block, paragraph, matched, class, context_hash, ruling, rationale,
    source_line}。ruling / rationale は未裁定のあいだ空。
    """
    work = pathlib.Path(work)
    meta = _load_chapters_meta_raw(work)
    blocks, chapters = _block_index(meta)
    records: list[dict] = []

    def _add(cid: str, n: int, para_text: str) -> None:
        for af in find_age_mentions(para_text):
            records.append({
                "block": _loc_block(blocks, cid, n) if cid else "",
                "paragraph": n,
                "matched": af.matched,
                "class": af.age_class,
                "context_hash": _hash_context(af.context),
                "ruling": "",
                "rationale": "",
                "source_line": para_text,
            })

    paras_files = sorted((work / "chapters").glob("ch*.paras.toml"))
    if paras_files:
        for f in paras_files:
            cid = f.name.split(".paras")[0]
            try:
                with open(f, "rb") as fh:
                    data = tomllib.load(fh)
            except (OSError, tomllib.TOMLDecodeError):
                continue
            for p in data.get("paras", []) or []:
                if (isinstance(p, dict) and isinstance(p.get("n"), int)
                        and isinstance(p.get("text"), str)):
                    _add(cid, p["n"], p["text"])
        return records

    src = work / "originals" / "source.md"
    if src.is_file():
        try:
            text = src.read_text(encoding="utf-8")
        except OSError:
            return records
        idx = 0
        for para in split_paras(text):
            idx += 1
            cid, n = _chapter_para(chapters, idx)
            _add(cid, n, para)
    return records


def render_age_policy_toml(records: list[dict]) -> str:
    """age_policy_report の TOML 文字列を作る（1件 = 8フィールド）。"""
    header = "\n".join([
        "# age_policy_report: 年齢表現の検出記録（機械生成）",
        "# 原文引用を含む正当な記録（残渣検査の対象外）。",
        "# 1件 = { block, paragraph, matched, class, context_hash,"
        " ruling, rationale, source_line }",
        "# class: age_diff（年齢差）/ past_time（過去の時点）/ "
        "other_person（他人）/ direct（本人）",
        "# 分類は確認の負担を減らすためのもので、安全確認から自動で外すものではない"
        "（全件を確認対象に残す）。",
        "# ruling / rationale は未裁定のあいだ空（裁定は rulings 移行に合わせる）。",
        "",
    ])
    return header + dump_toml({"mentions": records})


def render_age_policy_md(data: dict) -> str:
    """age_policy_report の TOML（パース済み dict）から md を生成する。"""
    mentions = [m for m in (data.get("mentions") or []) if isinstance(m, dict)]
    counts: dict[str, int] = {k: 0 for k in AGE_CLASSES}
    for m in mentions:
        k = str(m.get("class") or "")
        counts[k] = counts.get(k, 0) + 1
    summary = " / ".join(f"{k} {v}件" for k, v in counts.items())
    lines = [
        "# 年齢表現の検出記録（age_policy_report）",
        "",
        "- 本mdは analysis/age_policy_report-[ネタバレあり].toml からの生成物。手で編集しない。",
        f"- 検出: {len(mentions)}件（{summary}）",
        "- 分類は確認の負担を減らすためのもので、安全確認から自動で外すものではない。全件を確認対象に残す。",
        "- 過去の時点（past_time）も対象外にしない。性的場面との関係は別途確認する。",
        "",
        "| 位置 | 該当語 | 分類 | 原文行 | 裁定 | 根拠 | context_hash |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in mentions:
        block = str(m.get("block") or "")
        try:
            loc = (block + " " if block else "") + f"P{int(m.get('paragraph')):03d}"
        except (TypeError, ValueError):
            loc = block
        src = str(m.get("source_line") or "").replace("|", "｜").replace("\n", " ")
        lines.append(
            f"| {loc} | {m.get('matched', '')} | {m.get('class', '')} | {src} "
            f"| {m.get('ruling', '')} | {m.get('rationale', '')} "
            f"| {m.get('context_hash', '')} |")
    lines.append("")
    return "\n".join(lines)


def write_age_policy_report(work: pathlib.Path,
                            records: list[dict] | None = None) -> dict:
    """analysis/age_policy_report-[ネタバレあり].toml を書き、md を生成する。

    検出が0件のときは何も書かない（既存の記録を消さない）。
    md は TOML の内容から生成する（手で編集しない生成物）。
    戻り値: {"toml": Path, "md": Path, "count": int, "written": bool}
    """
    work = pathlib.Path(work)
    if records is None:
        records = build_age_policy_records(work)
    adir = work / "analysis"
    toml_path = adir / AGE_REPORT_TOML
    md_path = adir / AGE_REPORT_MD
    result = {"toml": toml_path, "md": md_path, "count": len(records),
              "written": False}
    if not records:
        return result
    toml_text = render_age_policy_toml(records)
    atomic_write_text(toml_path, toml_text)
    data = tomllib.loads(toml_text)
    atomic_write_text(md_path, render_age_policy_md(data))
    result["written"] = True
    return result


# ── CLI ─────────────────────────────────────────────────────────────────

def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="zh→ja 残渣検出: 日本語訳に残った中国語語彙・簡体字を検出")
    parser.add_argument("path", nargs="+", help="ファイルまたはディレクトリ")
    parser.add_argument("--json", action="store_true",
                        help="JSON 形式で機械可読出力")
    args = parser.parse_args()

    all_findings: list[dict] = []

    for p in args.path:
        p_obj = pathlib.Path(p)
        if p_obj.is_dir():
            for f in sorted(p_obj.rglob("*")):
                if f.suffix in (".toml", ".md", ".txt"):
                    all_findings.extend(_scan_file(f))
        elif p_obj.is_file():
            all_findings.extend(_scan_file(p_obj))

    seen = len(all_findings)

    if args.json:
        import json
        print(json.dumps(all_findings, ensure_ascii=False, indent=2))
    else:
        if seen == 0:
            print("✅ 検出なし")
        else:
            print(f"🔍 {seen} 件検出")
            for fd in all_findings[:10]:
                ctx = fd["context"][:60]
                sf = fd.get("safe_fix") or ""
                print(f"  L{fd['line']} [{fd['rule']}] "
                      f"{fd['value']}{' → ' + sf if sf else ''}: {ctx}")
            if seen > 10:
                print(f"  … 他 {seen - 10} 件（--json で全件表示）")

    return 1 if seen else 0


def _scan_file(fpath: pathlib.Path) -> list[dict]:
    fpath = pathlib.Path(fpath)
    if fpath.suffix == ".toml":
        findings = find_cn_residue_toml(fpath)
    else:
        findings = find_zh_vocab(fpath.read_text(encoding="utf-8"))
    name = fpath.relative_to(pathlib.Path.cwd()) if fpath.is_relative_to(
        pathlib.Path.cwd()) else fpath
    return [{
        "file": str(name),
        "rule": f.rule,
        "value": f.value,
        "context": f.context,
        "line": f.line,
        "safe_fix": f.safe_fix,
    } for f in findings]


if __name__ == "__main__":
    sys.exit(_main())
