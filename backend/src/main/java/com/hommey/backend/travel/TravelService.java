package com.hommey.backend.travel;

import com.hommey.backend.common.ApiException;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class TravelService {
  private static final List<String> REQUIRED =
      List.of("home_location", "transportation_preference", "hotel_brands", "seat_preference");
  private static final Set<String> EMPTY = Set.of("", "不指定", "暂不指定", "无", "没有");
  private final TravelRepository repository;

  public TravelService(TravelRepository repository) {
    this.repository = repository;
  }

  public Map<String, Object> preferences(String user) {
    return repository.preferences(user);
  }

  public Map<String, Object> onboarding(String user) {
    var prefs = preferences(user);
    var missing =
        REQUIRED.stream()
            .filter(k -> !prefs.containsKey(k) || EMPTY.contains(prefs.get(k).toString()))
            .toList();
    return Map.of(
        "is_new",
        !missing.isEmpty(),
        "completed",
        missing.isEmpty(),
        "completed_keys",
        REQUIRED.stream().filter(k -> !missing.contains(k)).toList(),
        "missing_keys",
        missing,
        "preferences",
        prefs);
  }

  @Transactional
  public Object answer(String user, String key, String value) {
    if (!REQUIRED.contains(key))
      throw new ApiException(400, "INVALID_ONBOARDING_PREFERENCE", "不支持的偏好字段");
    value = value.strip();
    boolean saved = !EMPTY.contains(value);
    if (saved) {
      repository.lockPreferences(user);
      Object stored = value;
      if (TravelRepository.LISTS.contains(key)) {
        var list = new ArrayList<Object>((List<?>) preferences(user).getOrDefault(key, List.of()));
        if (!list.contains(value)) list.add(value);
        stored = list;
      }
      repository.preference(user, key, stored);
    }
    var result = new LinkedHashMap<>(onboarding(user));
    result.putAll(
        Map.of(
            "success",
            true,
            "saved",
            saved,
            "key",
            key,
            "value",
            value,
            "message",
            saved ? "已记录偏好。" : "已跳过该偏好设置。"));
    return result;
  }

  public Map<String, Object> trip(String user, String session) {
    return repository.trip(user, session, false);
  }

  public List<Map<String, Object>> searchTrips(String user, String keyword, int limit) {
    return repository.searchTrips(user, keyword, limit);
  }
}
