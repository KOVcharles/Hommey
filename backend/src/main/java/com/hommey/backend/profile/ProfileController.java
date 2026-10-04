package com.hommey.backend.profile;

import com.hommey.backend.security.AccessPolicy;
import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/{user}/profile")
public class ProfileController {
  public record Save(@Valid @NotNull PersonalProfile profile, @NotNull @Min(0) Integer revision) {}

  public record Skip(@NotNull @Min(0) Integer revision) {}

  private final ProfileService profiles;
  private final AccessPolicy access;

  public ProfileController(ProfileService profiles, AccessPolicy access) {
    this.profiles = profiles;
    this.access = access;
  }

  @GetMapping
  public Object get(@PathVariable String user, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return profiles.get(user);
  }

  @PutMapping
  public Object save(
      @PathVariable String user, @Valid @RequestBody Save body, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return profiles.save(user, body.profile(), body.revision());
  }

  @PostMapping("/skip")
  public Object skip(
      @PathVariable String user, @Valid @RequestBody Skip body, @AuthenticationPrincipal Jwt jwt) {
    access.requireUser(jwt, user);
    return profiles.skip(user, body.revision());
  }
}
