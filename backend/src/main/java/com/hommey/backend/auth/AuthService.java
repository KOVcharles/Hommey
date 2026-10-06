package com.hommey.backend.auth;

import cn.dev33.satoken.stp.StpUtil;
import cn.dev33.satoken.stp.parameter.SaLoginParameter;
import com.hommey.backend.common.ApiException;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class AuthService {
  private final UserRepository users;
  private final PasswordEncoder passwords;
  private final RegistrationService registration;
  private final String dummy;

  public AuthService(
      UserRepository users, PasswordEncoder passwords, RegistrationService registration) {
    this.users = users;
    this.passwords = passwords;
    this.registration = registration;
    this.dummy = passwords.encode("constant-time-dummy");
  }

  public Object login(AuthController.Login body) {
    var user = users.byEmail(body.email().strip()).orElse(null);
    boolean valid = passwords.matches(body.password(), user == null ? dummy : user.passwordHash());
    if (!valid || user == null) throw new ApiException(401, "UNAUTHORIZED", "邮箱或密码错误");
    StpUtil.login(
        Long.toString(user.id()),
        new SaLoginParameter().setDeviceType(body.device() == null ? "web" : body.device()));
    return Map.of(
        "access_token", StpUtil.getTokenValue(),
        "token_type", "bearer",
        "expires_in", StpUtil.getTokenTimeout(),
        "user", Map.of("id", user.id(), "email", user.email(), "role", user.role()));
  }

  @Transactional
  public Object register(AuthController.Register body) {
    if (body.password().getBytes(StandardCharsets.UTF_8).length > 72)
      throw new ApiException(400, "BAD_REQUEST", "密码最长为72字节");
    String email = body.email().strip(), hash = RegistrationService.inviteHash(body.inviteCode());
    if (!users.inviteAvailable(hash))
      throw new ApiException(400, "INVALID_INVITE_CODE", "邀请码无效或已使用");
    registration.verifyCode(email, body.code());
    try {
      var user = users.create(email, passwords.encode(body.password()), registration.role(email));
      if (!users.consumeInvite(hash, user.id()))
        throw new ApiException(400, "INVALID_INVITE_CODE", "邀请码无效或已使用");
      return Map.of("id", user.id(), "email", user.email());
    } catch (DataIntegrityViolationException ex) {
      throw new ApiException(409, "EMAIL_ALREADY_EXISTS", "该邮箱已注册");
    }
  }
}
