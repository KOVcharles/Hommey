package com.hommey.backend.travel;

import com.hommey.backend.common.*;
import java.util.*;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

@Repository
public class TravelRepository {
  public static final Set<String> SCALARS =
      Set.of(
          "home_location",
          "transportation_preference",
          "seat_preference",
          "meal_preference",
          "budget_level");
  public static final Set<String> LISTS = Set.of("hotel_brands", "airlines");
  private final JdbcClient jdbc;
  private final JsonCodec json;

  public TravelRepository(JdbcClient jdbc, JsonCodec json) {
    this.jdbc = jdbc;
    this.json = json;
  }

  public Map<String, Object> preferences(String user) {
    var rows =
        jdbc.sql("SELECT * FROM user_travel_preferences WHERE user_id=:user")
            .param("user", user)
            .query()
            .listOfRows();
    if (rows.isEmpty()) return new LinkedHashMap<>();
    var row = rows.getFirst();
    var result = json.object(row.get("extra_preferences"));
    for (String key : SCALARS) if (row.get(key) != null) result.put(key, row.get(key));
    for (String key : LISTS) {
      Object value = json.read(row.get(key));
      if (value instanceof List<?> list && !list.isEmpty()) result.put(key, value);
    }
    return result;
  }

  public void preference(String user, String key, Object value) {
    validatePreference(key, value);
    String parameter = LISTS.contains(key) ? "CAST(:value AS jsonb)" : ":value";
    jdbc.sql(
            "INSERT INTO user_travel_preferences(user_id,"
                + key
                + ",preference_updated_at) VALUES(:user,"
                + parameter
                + ",jsonb_build_object(:key::text,to_jsonb(NOW()))) "
                + "ON CONFLICT(user_id) DO UPDATE SET "
                + key
                + "=EXCLUDED."
                + key
                + ",preference_updated_at=user_travel_preferences.preference_updated_at||EXCLUDED.preference_updated_at,updated_at=NOW()")
        .param("user", user)
        .param("key", key)
        .param("value", LISTS.contains(key) ? json.write(value) : value)
        .update();
    jdbc.sql(
            "INSERT INTO user_preferences(user_id,pref_type,pref_value) VALUES(:user,:key,CAST(:value AS jsonb)) ON CONFLICT(user_id,pref_type) DO UPDATE SET pref_value=EXCLUDED.pref_value,updated_at=NOW()")
        .param("user", user)
        .param("key", key)
        .param("value", json.write(value))
        .update();
  }

  public static void validatePreference(String key, Object value) {
    if (value != null && PreferenceValues.sensitive(value))
      throw new ApiException(400, "INVALID_PREFERENCE", "敏感信息不能保存为偏好");
    if (!SCALARS.contains(key) && !LISTS.contains(key))
      throw new ApiException(400, "INVALID_PREFERENCE", "不支持的偏好字段");
    if (SCALARS.contains(key) && (!(value instanceof String s) || s.isBlank() || s.length() > 200))
      throw new ApiException(400, "INVALID_PREFERENCE", "偏好值无效");
    if (LISTS.contains(key)
        && (!(value instanceof List<?> list)
            || list.size() > 30
            || list.stream()
                .anyMatch(v -> !(v instanceof String s) || s.isBlank() || s.length() > 120)))
      throw new ApiException(400, "INVALID_PREFERENCE", "偏好列表无效");
  }

  public void lockPreferences(String user) {
    jdbc.sql("INSERT INTO user_travel_preferences(user_id) VALUES(:user) ON CONFLICT DO NOTHING")
        .param("user", user)
        .update();
    jdbc.sql("SELECT user_id FROM user_travel_preferences WHERE user_id=:user FOR UPDATE")
        .param("user", user)
        .query(String.class)
        .single();
  }

  public Map<String, Object> trip(String user, String session, boolean lock) {
    var rows =
        jdbc.sql(
                "SELECT status,context_data FROM active_trip_contexts WHERE user_id=:user AND session_id=:session"
                    + (lock ? " FOR UPDATE" : ""))
            .param("user", user)
            .param("session", session)
            .query()
            .listOfRows();
    if (rows.isEmpty() || Set.of("completed", "cancelled").contains(rows.getFirst().get("status")))
      return new LinkedHashMap<>();
    var trip = json.object(rows.getFirst().get("context_data"));
    trip.put("status", rows.getFirst().get("status"));
    return trip;
  }

  public void saveTrip(String user, String session, Map<String, Object> trip) {
    jdbc.sql(
            """
            INSERT INTO active_trip_contexts(user_id,session_id,status,context_data)
            VALUES(:user,:session,:status,CAST(:trip AS jsonb)) ON CONFLICT(user_id,session_id) DO UPDATE
            SET status=EXCLUDED.status,context_data=EXCLUDED.context_data,updated_at=NOW(),
              completed_at=CASE WHEN EXCLUDED.status='cancelled' THEN NOW() ELSE NULL END
            """)
        .param("user", user)
        .param("session", session)
        .param("status", trip.get("status"))
        .param("trip", json.write(trip))
        .update();
    int changed =
        jdbc.sql(
                """
            INSERT INTO supervisor_trip_records(trip_id,user_id,session_id,record_state,context_data)
            VALUES(:id,:user,:session,:state,CAST(:trip AS jsonb)) ON CONFLICT(trip_id) DO UPDATE
            SET record_state=EXCLUDED.record_state,context_data=EXCLUDED.context_data,updated_at=NOW()
            WHERE supervisor_trip_records.user_id=EXCLUDED.user_id AND supervisor_trip_records.session_id=EXCLUDED.session_id
            """)
            .param("id", trip.get("_trip_id"))
            .param("user", user)
            .param("session", UUID.fromString(session))
            .param("state", "cancelled".equals(trip.get("status")) ? "cancelled" : "planned")
            .param("trip", json.write(trip))
            .update();
    if (changed != 1) throw new ApiException(403, "FORBIDDEN", "行程归属不一致");
  }

  public List<Map<String, Object>> searchTrips(String user, String keyword, int limit) {
    String pattern =
        "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%";
    return jdbc
        .sql(
            "SELECT trip_id,record_state,context_data,updated_at FROM supervisor_trip_records WHERE user_id=:user AND context_data::text ILIKE :pattern ORDER BY updated_at DESC LIMIT :limit")
        .param("user", user)
        .param("pattern", pattern)
        .param("limit", limit)
        .query()
        .listOfRows()
        .stream()
        .map(
            row -> {
              var data = new LinkedHashMap<>(row);
              data.put("context_data", json.read(row.get("context_data")));
              data.put("updated_at", row.get("updated_at").toString());
              return Map.<String, Object>of(
                  "kind", "trip", "record_state", row.get("record_state"), "data", data);
            })
        .toList();
  }
}
