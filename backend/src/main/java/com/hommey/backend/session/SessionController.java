package com.hommey.backend.session;

import com.hommey.backend.security.AccessPolicy;
import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import java.util.Map;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/{user}")
public class SessionController {
  public record Rename(@NotBlank @Size(max = 80) String title) {}

  private final SessionService sessions;
  private final AccessPolicy access;

  public SessionController(SessionService sessions, AccessPolicy access) {
    this.sessions = sessions;
    this.access = access;
  }

  @GetMapping("/sessions")
  public Object list(@PathVariable String user, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return sessions.list(user);
  }

  @PostMapping("/sessions")
  public Object create(@PathVariable String user, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return sessions.create(user);
  }

  @GetMapping("/sessions/{session}")
  public Object get(
      @PathVariable String user, @PathVariable String session, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return sessions.get(user, session);
  }

  @PostMapping("/sessions/{session}/activate")
  public Object activate(
      @PathVariable String user, @PathVariable String session, @AuthenticationPrincipal Jwt jwt) {
    return get(user, session, jwt);
  }

  @PatchMapping("/sessions/{session}")
  public Object rename(
      @PathVariable String user,
      @PathVariable String session,
      @Valid @RequestBody Rename body,
      @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    sessions.rename(user, session, body.title());
    return Map.of("session_id", session, "title", body.title());
  }

  @DeleteMapping("/sessions/{session}")
  public Object delete(
      @PathVariable String user, @PathVariable String session, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    sessions.delete(user, session);
    return Map.of("session_id", session, "deleted", true);
  }

  @DeleteMapping("/history")
  public Object clear(@PathVariable String user, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    sessions.clear(user);
    return Map.of("cleared", true);
  }
}
