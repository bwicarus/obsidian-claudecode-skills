import Foundation
import CoreFoundation

/// The existing voice settings vocabulary, rendered and validated natively.
/// Network model choices still come from the server catalog; these fields
/// describe the already-supported voice protocol, not new product options.
enum ReaderNativeVoiceSettings {
    typealias O = [String:Any]
    static func fields(_ cfg: O, device: [String:String]) -> [O] {
        var fields: [O] = []
        func add(_ key: String, _ label: String, _ kind: String, _ fallback: Any, _ extra: O = [:]) {
            var row: O = ["key":key,"label":label,"kind":kind,"value":cfg[key].flatMap { $0 is NSNull ? nil : $0 } ?? fallback,"section":"通话"]
            row.merge(extra) { _,new in new }; fields.append(row)
        }
        func choice(_ key: String, _ label: String, _ options: [[String]], _ fallback: String, _ extra: O = [:]) {
            var data = extra; data["options"] = options.map { ["value":$0[0],"label":$0.count > 1 ? $0[1] : $0[0]] }
            add(key,label,"choice",fallback,data)
        }
        func values(_ names: [String]) -> [[String]] { names.map { [$0] } }
        func range(_ key: String, _ label: String, _ value: Double, _ min: Double, _ max: Double, _ step: Double, _ extra: O = [:]) {
            var data = extra; data["min"] = min; data["max"] = max; data["step"] = step; add(key,label,"range",value,data)
        }
        let raw = cfg["rt_engine"] as? String ?? "", engine = raw == "openai" ? "openai_rtc" : raw == "computer_client" ? "" : raw
        choice("rt_engine","普通语音引擎",[["","豆包 S2S"],["openai_rtc","GPT Realtime"],["grok","Grok Voice"]],"")
        fields[0]["value"] = engine
        if engine == "openai_rtc" {
            choice("rt_model","模型",values(["gpt-realtime-2.1-mini","gpt-realtime-2.1"]),"gpt-realtime-2.1-mini")
            choice("rt_voice","声音",values(["marin","cedar","alloy","ash","ballad","coral","echo","sage","shimmer","verse"]),"marin")
            range("rt_speed","通话语速",1,0.5,1.5,0.05)
            choice("rt_lang","语言",[["","自动"],["zh","中文"],["ja","日本語"],["en","English"]],"")
            choice("rt_noise","噪音抑制",[["","近场：耳机或手持"],["far","远场：桌面外放"]],"")
            choice("rt_eagerness","接话灵敏度",[["auto","自动"],["low","多等一下"],["medium","适中"],["high","尽快接话"]],"auto")
            choice("rt_effort","思考强度",values(["minimal","low","medium","high"]),"low")
            add("rt_instructions","附加指令","text","")
            add("rt_full_duplex","允许说话时打断（耳机）","toggle",false)
            add("rt_image","图像输入","toggle",false)
            add("rt_tool_reply","工具完成后口头回报","toggle",false)
        } else {
            if engine == "grok" {
                choice("rt_grok_voice","Grok 声音",values(["eve","ara","rex","sal","leo"]),"eve")
                choice("rt_grok_vad","轮次判定",[["","本地 VAD"],["server","服务端 VAD"]],"")
            }
            choice("speaker","豆包声音",speakers,speakers[0][0])
            choice("explicit_dialect","方言",[["","标准(无方言)"],["dongbei","东北话"],["sichuan","四川话"],["shaanxi","陕西话"]],"")
            range("speech_rate","豆包语速",0,-50,100,5); range("loudness_rate","豆包音量",0,-50,100,5)
            add("bot_name","助手名字","text",""); add("speaking_style","说话风格","text",""); add("system_role","背景设定","text","")
            add("enable_music","唱歌能力","toggle",false)
        }
        let section: O = ["section":"朗读与输入"]
        choice("tts_speaker","朗读声音",ttsSpeakers,ttsSpeakers[0][0],section)
        range("tts_speech_rate","朗读语速",0,-50,100,5,section)
        add("tts_instruction","默认朗读语气","text","",section); add("asr_v2","ASR 2.0（需已开通）","toggle",false,section)
        for spec in deviceSpecifications {
            let raw = device[spec[0]] ?? spec[3]
            var row: O = ["key":spec[0],"label":spec[1],"kind":spec[2],"section":"本设备","device":true]
            if spec[2] == "toggle" { row["value"] = raw == "1" }
            else if spec[2] == "range" { row["value"] = Double(raw) ?? 20; row["min"] = 5; row["max"] = 60; row["step"] = 5 }
            else { row["value"] = raw; row["options"] = [["value":"auto","label":"自动"],["value":"1","label":"总是"],["value":"0","label":"关闭"]] }
            if spec[0] == "rc-voice-cue" { row["disabled"] = cfg["rt_tool_reply"] as? Bool == true }
            fields.append(row)
        }
        return fields
    }

    static func validate(_ field: O, _ value: Any) throws {
        let kind = field["kind"] as? String ?? ""
        var valid = false
        if field["disabled"] as? Bool != true {
            switch kind {
            case "toggle": if let n = value as? NSNumber { valid = CFGetTypeID(n) == CFBooleanGetTypeID() }
            case "choice": valid = (field["options"] as? [[String:String]] ?? []).contains { $0["value"] == value as? String }
            case "range": if let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), let min = field["min"] as? NSNumber, let max = field["max"] as? NSNumber { valid = n.doubleValue.isFinite && n.doubleValue >= min.doubleValue && n.doubleValue <= max.doubleValue }
            default: valid = (value as? String).map { $0.utf16.count <= 16_000 } ?? false
            }
        }
        guard valid else { throw ReaderNativePreferences.Failure(message:"设置值无效或当前不可修改") }
    }

    static let deviceSpecifications = [
        ["asst-followups-on","显示追问建议","toggle","0"], ["rc-voice-sub","朗读字幕","toggle","1"],
        ["rc-voice-bridge","回声桥","choice","auto"], ["rc-voice-card-hide","文字卡自动收起","toggle","1"],
        ["rc-voice-card-secs","文字卡停留秒数","range","20"], ["rc-voice-cue","任务完成提示音","toggle","1"]]
    static let speakers = [
        ["zh_female_vv_jupiter_bigtts","vv · 活泼灵动女声(默认,支持方言)"],
        ["zh_female_xiaohe_jupiter_bigtts","xiaohe · 甜美女声(台湾腔)"],
        ["zh_male_yunzhou_jupiter_bigtts","yunzhou · 清爽沉稳男声"],
        ["zh_male_xiaotian_jupiter_bigtts","xiaotian · 清爽磁性男声"],
        ["en_male_tim_uranus_bigtts","Tim · 美式英语男声"],
        ["en_female_dacey_uranus_bigtts","Dacey · 美式英语女声"],
        ["en_female_stokie_uranus_bigtts","Stokie · 美式英语女声"]]
    static let ttsSpeakers = [
        ["zh_female_vv_uranus_bigtts","vv · 2.0(推荐,支持语气指令)"], ["zh_female_shuangkuaisisi_uranus_bigtts","爽快思思 · 2.0"],
        ["zh_male_yuanboxiaoshu_uranus_bigtts","渊博小叔 · 2.0(讲解风)"], ["zh_male_shenyeboke_uranus_bigtts","深夜播客 · 2.0"],
        ["zh_female_wenrouxiaoya_uranus_bigtts","温柔小雅 · 2.0"], ["zh_male_ruyaqingnian_uranus_bigtts","儒雅青年 · 2.0"],
        ["zh_female_qinqienv_uranus_bigtts","亲切女声 · 2.0"], ["zh_female_shuangkuaisisi_moon_bigtts","爽快思思(1.0,中英双语)"],
        ["zh_male_wennuanahu_moon_bigtts","温暖阿虎(中英双语)"], ["zh_male_shaonianzixin_moon_bigtts","少年梓辛(中英双语)"],
        ["zh_male_yuanboxiaoshu_moon_bigtts","渊博小叔(讲解风)"], ["zh_male_jieshuoxiaoming_moon_bigtts","解说小明"],
        ["zh_male_shenyeboke_moon_bigtts","深夜播客"], ["zh_female_qinqienvsheng_moon_bigtts","亲切女声"],
        ["zh_female_linjianvhai_moon_bigtts","邻家女孩"], ["zh_female_kailangjiejie_moon_bigtts","开朗姐姐"],
        ["zh_female_gaolengyujie_moon_bigtts","高冷御姐"], ["zh_female_wanwanxiaohe_moon_bigtts","湾湾小何(台湾腔)"],
        ["en_female_lauren_moon_bigtts","Lauren(纯英语)"]]
}
