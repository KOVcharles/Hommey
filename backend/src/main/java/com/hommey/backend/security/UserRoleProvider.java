package com.hommey.backend.security;

import cn.dev33.satoken.stp.StpInterface;
import com.hommey.backend.auth.UserRepository;
import java.util.List;
import org.springframework.stereotype.Component;

@Component
public class UserRoleProvider implements StpInterface {
  private final UserRepository users;

  public UserRoleProvider(UserRepository users) {
    this.users = users;
  }

  @Override
  public List<String> getRoleList(Object loginId, String loginType) {
    return users.byId(loginId.toString()).map(user -> List.of(user.role())).orElse(List.of());
  }

  @Override
  public List<String> getPermissionList(Object loginId, String loginType) {
    return List.of();
  }
}
