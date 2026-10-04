package com.hommey.backend.auth;

import com.hommey.backend.common.ApiException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.*;
import java.util.*;
import org.altcha.altcha.v2.Altcha;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;

@Service
public class RegistrationService {
  private final RegistrationProperties props;
  private final UserRepository users;
  private final StringRedisTemplate redis;
  private final RestClient mail;
  private final SecureRandom random = new SecureRandom();

  public RegistrationService(
      RegistrationProperties props,
      UserRepository users,
      StringRedisTemplate redis,
      RestClient.Builder builder) {
    this.props = props;
    this.users = users;
    this.redis = redis;
    var factory = new JdkClientHttpRequestFactory();
    factory.setReadTimeout(Duration.ofSeconds(10));
    this.mail = builder.requestFactory(factory).build();
  }

  private String secret() {
    if (props.altchaSecret() == null || props.altchaSecret().isBlank())
      throw new ApiException(503, "ALTCHA_NOT_CONFIGURED", "人机验证尚未配置");
    return props.altchaSecret();
  }

  public Object challenge() {
    try {
      var challenge =
          Altcha.createChallenge(
              new Altcha.CreateChallengeOptions()
                  .algorithm("SHA-256")
                  .cost(1)
                  .hmacSignatureSecret(secret())
                  .expiresInSeconds(300));
      // Use the protocol serializer: the widget requires camelCase parameters,
      // independent of our business DTO naming strategy.
      return new org.json.JSONObject(challenge.toJson()).toMap();
    } catch (ApiException ex) {
      throw ex;
    } catch (Exception ex) {
      throw new ApiException(503, "ALTCHA_UNAVAILABLE", "人机验证暂不可用");
    }
  }

  public void send(AuthController.SendCode body, String ip) {
    String nonce;
    try {
      if (!Altcha.verifySolution(body.altcha(), secret(), Altcha.kdf("SHA-256")).verified())
        throw new ApiException(400, "INVALID_ALTCHA", "人机验证未通过");
      nonce = Altcha.parsePayload(body.altcha()).challenge().parameters().nonce();
    } catch (ApiException ex) {
      throw ex;
    } catch (Exception ex) {
      throw new ApiException(400, "INVALID_ALTCHA", "人机验证未通过");
    }
    if (!Boolean.TRUE.equals(
        redis
            .opsForValue()
            .setIfAbsent("hommey:auth:captcha:" + digest(nonce), "1", Duration.ofMinutes(5))))
      throw new ApiException(400, "INVALID_ALTCHA", "请重新完成人机验证");
    String email = body.email().strip(), subject = digest(email);
    limit("ip:" + digest(ip) + ":" + Instant.now().getEpochSecond() / 60, 5, 120);
    if (users.byEmail(email).isPresent()) return;
    if (!users.inviteAvailable(inviteHash(body.inviteCode())))
      throw new ApiException(400, "INVALID_INVITE_CODE", "邀请码无效或已使用");
    if (!Boolean.TRUE.equals(
        redis
            .opsForValue()
            .setIfAbsent("hommey:auth:cooldown:" + subject, "1", Duration.ofSeconds(60))))
      throw new ApiException(429, "TOO_MANY_REQUESTS", "验证码发送过于频繁");
    limit("daily:" + subject + ":" + LocalDate.now(ZoneId.of("Asia/Shanghai")), 10, 172800);
    if (props.resendApiKey() == null || props.resendApiKey().isBlank())
      throw new ApiException(503, "EMAIL_NOT_CONFIGURED", "邮件服务尚未配置");
    String code = String.format("%06d", random.nextInt(1_000_000));
    redis
        .opsForValue()
        .set("hommey:auth:code:" + subject, digest(secret() + ":" + code), Duration.ofMinutes(5));
    redis.delete("hommey:auth:attempts:" + subject);
    try {
      mail.post()
          .uri(props.resendUrl())
          .header("Authorization", "Bearer " + props.resendApiKey())
          .header("Idempotency-Key", UUID.randomUUID().toString())
          .body(
              Map.of(
                  "from",
                  props.resendFrom(),
                  "to",
                  List.of(email),
                  "subject",
                  "Hommey 注册验证码",
                  "text",
                  "你的验证码是 " + code + "，5分钟内有效。"))
          .retrieve()
          .toBodilessEntity();
    } catch (Exception ex) {
      throw new ApiException(502, "EMAIL_SEND_FAILED", "验证码邮件发送失败，请稍后重试");
    }
  }

  private void limit(String suffix, int maximum, int ttl) {
    var script =
        new DefaultRedisScript<Long>(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n",
            Long.class);
    Long n = redis.execute(script, List.of("hommey:auth:rate:" + suffix), Integer.toString(ttl));
    if (n == null || n > maximum) throw new ApiException(429, "TOO_MANY_REQUESTS", "请求过于频繁，请稍后重试");
  }

  public void verifyCode(String email, String code) {
    String subject = digest(email);
    var script =
        new DefaultRedisScript<Long>(
            """
            local code=redis.call('GET',KEYS[1]); if not code then return 0 end
            local n=redis.call('INCR',KEYS[2]); if n==1 then redis.call('EXPIRE',KEYS[2],300) end
            if n>5 then redis.call('DEL',KEYS[1],KEYS[2]); return -1 end
            if code~=ARGV[1] then return 0 end
            redis.call('DEL',KEYS[1],KEYS[2]); return 1
            """,
            Long.class);
    Long result =
        redis.execute(
            script,
            List.of("hommey:auth:code:" + subject, "hommey:auth:attempts:" + subject),
            digest(secret() + ":" + code));
    if (result == null || result != 1)
      throw new ApiException(400, "INVALID_VERIFICATION_CODE", "验证码无效或已过期");
  }

  public String role(String email) {
    return Arrays.stream(props.adminEmails().split(","))
            .map(String::strip)
            .anyMatch(email::equalsIgnoreCase)
        ? "admin"
        : "user";
  }

  public static String inviteHash(String code) {
    return digest(code.replaceAll("\\s", "").replace("-", "").toUpperCase(Locale.ROOT));
  }

  public static String digest(String text) {
    try {
      return HexFormat.of()
          .formatHex(
              MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8)));
    } catch (Exception ex) {
      throw new IllegalStateException(ex);
    }
  }
}
