package com.hommey.backend.profile;

import java.util.Map;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class ProfileService {
  private final ProfileRepository repository;

  public ProfileService(ProfileRepository repository) {
    this.repository = repository;
  }

  public Map<String, Object> get(String user) {
    return repository.get(user);
  }

  @Transactional
  public Map<String, Object> save(String user, PersonalProfile profile, int revision) {
    return repository.write(user, profile, "completed", revision);
  }

  @Transactional
  public Map<String, Object> skip(String user, int revision) {
    return repository.write(user, null, "skipped", revision);
  }
}
