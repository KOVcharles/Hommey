package com.hommey.backend.security;

import com.hommey.backend.auth.UserRepository;
import com.hommey.backend.common.ApiException;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.stereotype.Component;

@Component
public class AccessPolicy {
  private final UserRepository users;

  public AccessPolicy(UserRepository users) {
    this.users = users;
  }

  public UserRepository.User requireUser(Jwt jwt, String userId) {
    if (jwt == null || !userId.equals(jwt.getSubject()))
      throw new ApiException(403, "FORBIDDEN", "无权访问该用户的数据");
    return users
        .byId(userId)
        .orElseThrow(() -> new ApiException(401, "UNAUTHORIZED", "用户不存在，请重新登录"));
  }

  public void requireAdmin(Jwt jwt) {
    if (!"admin".equals(requireUser(jwt, jwt.getSubject()).role()))
      throw new ApiException(403, "FORBIDDEN", "仅管理员可以使用此功能");
  }

  public void requireSession(Jwt jwt, String sessionId) {
    if (!sessionId.equals(jwt.getClaimAsString("session_id")))
      throw new ApiException(403, "FORBIDDEN", "执行凭证不属于此会话");
  }
}
