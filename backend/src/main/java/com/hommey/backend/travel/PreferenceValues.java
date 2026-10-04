package com.hommey.backend.travel;

import java.util.List;
import java.util.regex.Pattern;

/** Existing preference storage rules; personal-profile identifiers have their own schema. */
final class PreferenceValues {
  private static final List<Pattern> SENSITIVE =
      List.of(
          Pattern.compile(
              "(?i)\\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|pwd|secret)\\s*[:=]\\s*['\"]?[^\\s,;，；'\"]{4,}"),
          Pattern.compile("(?:密码|口令|令牌|密钥)\\s*(?:是|为|[:：=])\\s*[^\\s,;，；]{4,}"),
          Pattern.compile("(?i)\\bBearer\\s+[A-Za-z0-9._~+/-]{8,}=*"),
          Pattern.compile("\\bsk-[A-Za-z0-9_-]{8,}\\b"),
          Pattern.compile("\\beyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\b"),
          Pattern.compile(
              "(?<![A-Za-z0-9_.+-])[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}(?![A-Za-z0-9_.-])"),
          Pattern.compile("(?<!\\d)(?:\\+?86[- ]?)?1[3-9]\\d{9}(?!\\d)"),
          Pattern.compile("(?<!\\d)\\d{17}[0-9Xx](?!\\d)"),
          Pattern.compile("(?<!\\d)(?:\\d[ -]?){16,19}(?!\\d)"),
          Pattern.compile(
              "(?i)(?:护照(?:号|号码)?|passport(?:\\s*(?:no|number))?)\\s*[:：]?\\s*[A-Z0-9]{5,17}"),
          Pattern.compile(
              "(?:[\\u4e00-\\u9fff]{2,}(?:路|街|巷|道|弄))\\s*\\d{1,5}\\s*(?:号|弄|栋|幢|单元|室)(?:[-\\d室单元楼层]*)"),
          Pattern.compile("(?i)(?:公司机密|商业秘密|绝密|confidential)\\s*[:：]?\\s*[^\\n]{1,200}"));

  private PreferenceValues() {}

  static boolean sensitive(Object value) {
    return SENSITIVE.stream().anyMatch(pattern -> pattern.matcher(value.toString()).find());
  }
}
