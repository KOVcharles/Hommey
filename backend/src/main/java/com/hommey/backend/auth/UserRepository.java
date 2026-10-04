package com.hommey.backend.auth;

import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

@Repository
public class UserRepository {
  public record User(long id, String email, String passwordHash, String role) {}

  private final JdbcClient jdbc;

  public UserRepository(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  public Optional<User> byEmail(String email) {
    return jdbc.sql("SELECT id,email,password_hash,role FROM users WHERE email=:email")
        .param("email", email)
        .query(User.class)
        .optional();
  }

  public Optional<User> byId(String id) {
    return jdbc.sql("SELECT id,email,password_hash,role FROM users WHERE id::text=:id")
        .param("id", id)
        .query(User.class)
        .optional();
  }

  public User create(String email, String hash, String role) {
    return jdbc.sql(
            "INSERT INTO users(email,password_hash,role) VALUES(:email,:hash,:role) RETURNING id,email,password_hash,role")
        .param("email", email)
        .param("hash", hash)
        .param("role", role)
        .query(User.class)
        .single();
  }

  public boolean inviteAvailable(String hash) {
    return jdbc.sql("SELECT COUNT(*) FROM invite_codes WHERE code_hash=:hash AND used_at IS NULL")
            .param("hash", hash)
            .query(Integer.class)
            .single()
        > 0;
  }

  public boolean consumeInvite(String hash, long user) {
    return jdbc.sql(
                "UPDATE invite_codes SET used_at=NOW(),used_by_user_id=:user WHERE code_hash=:hash AND used_at IS NULL")
            .param("hash", hash)
            .param("user", user)
            .update()
        == 1;
  }
}
