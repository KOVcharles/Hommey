package com.hommey.backend.agent;

import com.hommey.backend.common.ApiException;
import com.hommey.backend.profile.ProfileService;
import com.hommey.backend.security.AccessPolicy;
import com.hommey.backend.session.SessionService;
import com.hommey.backend.travel.TravelService;
import jakarta.validation.Valid;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.*;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/internal/business/users/{user}")
public class BusinessController {
  private final AccessPolicy access;
  private final ProfileService profiles;
  private final SessionService sessions;
  private final TravelService travel;
  private final MutationService mutations;

  public BusinessController(
      AccessPolicy access,
      ProfileService profiles,
      SessionService sessions,
      TravelService travel,
      MutationService mutations) {
    this.access = access;
    this.profiles = profiles;
    this.sessions = sessions;
    this.travel = travel;
    this.mutations = mutations;
  }

  @GetMapping("/preferences")
  public Object preferences(@PathVariable String user, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return travel.preferences(user);
  }

  @GetMapping("/profile")
  public Object profile(@PathVariable String user, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return profiles.get(user);
  }

  @GetMapping("/sessions/{session}")
  public Object session(
      @PathVariable String user, @PathVariable String session, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    access.requireSession(jwt, session);
    return sessions.get(user, session);
  }

  @GetMapping("/messages")
  public Object messages(
      @PathVariable String user,
      @RequestParam("session_id") String session,
      @RequestParam(defaultValue = "1000") int limit,
      @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    access.requireSession(jwt, session);
    return sessions.messages(user, session, limit, null);
  }

  @GetMapping("/context")
  public Object context(
      @PathVariable String user,
      @RequestParam("session_id") String session,
      @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    access.requireSession(jwt, session);
    return Map.of(
        "messages",
        sessions.messages(user, session, 100, jwt.getClaimAsString("request_id")),
        "trip",
        travel.trip(user, session),
        "current_excluded",
        true,
        "today",
        LocalDate.now(ZoneId.of("Asia/Shanghai")).toString());
  }

  @PostMapping("/messages")
  public Object append(
      @PathVariable String user,
      @Valid @RequestBody SessionService.Message body,
      @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    String session = jwt.getClaimAsString("session_id"),
        request = jwt.getClaimAsString("request_id");
    if (session == null || request == null) throw new ApiException(403, "FORBIDDEN", "执行凭证缺少会话或请求");
    return sessions.append(user, session, request, body);
  }

  @PostMapping("/mutations")
  public Object apply(
      @PathVariable String user,
      @Valid @RequestBody MutationService.Mutation body,
      @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    if (jwt.getClaimAsString("session_id") == null || jwt.getClaimAsString("request_id") == null)
      throw new ApiException(403, "FORBIDDEN", "执行凭证缺少会话或请求");
    return mutations.apply(
        user, jwt.getClaimAsString("session_id"), jwt.getClaimAsString("request_id"), body);
  }

  @GetMapping("/memory")
  public Object memory(
      @PathVariable String user,
      @RequestParam(defaultValue = "all") String kind,
      @RequestParam(defaultValue = "") String query,
      @RequestParam(defaultValue = "10") int limit,
      @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    limit = Math.min(30, Math.max(1, limit));
    if (query.length() > 500) throw new ApiException(400, "BAD_REQUEST", "检索条件过长");
    var results = new ArrayList<Map<String, Object>>();
    if (Set.of("all", "preferences").contains(kind))
      results.add(Map.of("kind", "preferences", "data", travel.preferences(user)));
    if (Set.of("all", "trips").contains(kind))
      results.addAll(travel.searchTrips(user, query, limit));
    if (Set.of("all", "messages").contains(kind)) {
      for (var row : sessions.messages(user, null, 1000, null))
        if (row.get("content")
            .toString()
            .toLowerCase(Locale.ROOT)
            .contains(query.toLowerCase(Locale.ROOT))) {
          results.add(Map.of("kind", "message", "data", row));
          if (results.size() >= limit) break;
        }
    }
    return results.stream().limit(limit).toList();
  }
}
