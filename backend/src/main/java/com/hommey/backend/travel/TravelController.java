package com.hommey.backend.travel;

import com.hommey.backend.security.AccessPolicy;
import com.hommey.backend.session.SessionService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import java.util.*;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/{user}")
public class TravelController {
  public record Preference(
      @NotBlank @Size(max = 80) String key, @NotNull @Size(max = 200) String value) {}

  private static final Map<String, List<String>> DISPLAY =
      Map.of(
          "home_location",
          List.of("常驻地", "📍"),
          "transportation_preference",
          List.of("出行偏好", "🚄"),
          "hotel_brands",
          List.of("常住酒店", "🏨"),
          "airlines",
          List.of("常用航空", "✈️"),
          "seat_preference",
          List.of("座位偏好", "💺"),
          "meal_preference",
          List.of("餐食偏好", "🍜"),
          "budget_level",
          List.of("预算等级", "💰"));
  private final TravelService travel;
  private final SessionService sessions;
  private final AccessPolicy access;

  public TravelController(TravelService travel, SessionService sessions, AccessPolicy access) {
    this.travel = travel;
    this.sessions = sessions;
    this.access = access;
  }

  @GetMapping("/onboarding")
  public Object onboarding(@PathVariable String user, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return travel.onboarding(user);
  }

  @PostMapping("/onboarding/preference")
  public Object preference(
      @PathVariable String user,
      @Valid @RequestBody Preference body,
      @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return travel.answer(user, body.key(), body.value());
  }

  @GetMapping("/is-new")
  public Object isNew(@PathVariable String user, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return Map.of("is_new", travel.onboarding(user).get("is_new"));
  }

  @GetMapping("/trip/active")
  public Object trip(
      @PathVariable String user,
      @RequestParam("session_id") String session,
      @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    sessions.require(user, session);
    var result = new LinkedHashMap<String, Object>();
    var trip = travel.trip(user, session);
    result.put("active_trip", trip.isEmpty() ? null : trip);
    return result;
  }

  @GetMapping("/summary")
  public Object summary(@PathVariable String user, @AuthenticationPrincipal Jwt jwt) {
    var account = access.requireUser(jwt, user);
    var preferences = travel.preferences(user);
    var display =
        preferences.entrySet().stream()
            .filter(
                e ->
                    e.getValue() != null
                        && !e.getValue().toString().isBlank()
                        && !List.of().equals(e.getValue()))
            .map(
                e -> {
                  var label = DISPLAY.getOrDefault(e.getKey(), List.of(e.getKey(), "📋"));
                  return Map.of(
                      "icon",
                      label.get(1),
                      "label",
                      label.get(0),
                      "value",
                      e.getValue() instanceof List<?> list
                          ? String.join(" · ", list.stream().map(Object::toString).toList())
                          : e.getValue().toString());
                })
            .toList();
    return Map.of(
        "user_id",
        user,
        "name_display",
        preferences.getOrDefault("name", user),
        "preferences",
        display,
        "role",
        account.role(),
        "initialized",
        true,
        "member_level",
        "白银会员",
        "member_tag",
        "差旅常客");
  }

  @GetMapping("/status")
  public Object status(@PathVariable String user, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    var result = new LinkedHashMap<String, Object>();
    result.put("initialized", true);
    result.put("error", null);
    return result;
  }

  @PostMapping("/init")
  public Object init(@PathVariable String user, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return Map.of("success", true, "initialized", true);
  }
}
