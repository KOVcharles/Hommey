package com.hommey.backend.agent;

import com.hommey.backend.common.*;
import com.hommey.backend.session.SessionService;
import com.hommey.backend.travel.TravelRepository;
import jakarta.validation.constraints.*;
import java.time.LocalDate;
import java.util.*;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class MutationService {
  public record Mutation(
      @NotBlank @Pattern(regexp = "[a-f0-9]{64}") String operationId,
      @NotBlank @Size(max = 128) String owner,
      @Min(0) long expectedVersion,
      @NotNull @Size(max = 16) Map<String, Object> trip,
      @NotNull @Size(max = 7) Map<String, Object> preferences,
      @NotBlank @Pattern(regexp = "update|new|cancel") String action) {}

  private static final Set<String> TRIP_FIELDS =
      Set.of(
          "origin",
          "destination",
          "start_date",
          "end_date",
          "duration_days",
          "trip_purpose",
          "work_location",
          "work_schedule",
          "work_location_verified",
          "_capability_selection");
  private final JdbcClient jdbc;
  private final TravelRepository travel;
  private final SessionService sessions;
  private final JsonCodec json;
  private final TripVersion versions;

  public MutationService(
      JdbcClient jdbc,
      TravelRepository travel,
      SessionService sessions,
      JsonCodec json,
      TripVersion versions) {
    this.jdbc = jdbc;
    this.travel = travel;
    this.sessions = sessions;
    this.json = json;
    this.versions = versions;
  }

  @Transactional
  public Object apply(String user, String session, String request, Mutation body) {
    jdbc.sql("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))")
        .param("key", "supervisor:" + user)
        .query()
        .listOfRows();
    var rows =
        jdbc.sql(
                "SELECT owner,status,cancel_requested FROM supervisor_runs WHERE user_id=:user AND session_id=:session AND request_id=:request FOR UPDATE")
            .param("user", user)
            .param("session", UUID.fromString(session))
            .param("request", request)
            .query()
            .listOfRows();
    if (rows.isEmpty() || !body.owner().equals(rows.getFirst().get("owner")))
      throw new ApiException(409, "RUN_STOPPED", "执行已失效");
    String hash =
        versions.hash(
            Map.of(
                "trip",
                body.trip(),
                "preferences",
                body.preferences(),
                "action",
                body.action(),
                "expected_version",
                body.expectedVersion()));
    var receipts =
        jdbc.sql(
                "SELECT payload_hash,receipt FROM business_operation_receipts WHERE user_id=:user AND request_id=:request AND operation_id=:operation")
            .param("user", user)
            .param("request", request)
            .param("operation", body.operationId())
            .query()
            .listOfRows();
    if (!receipts.isEmpty()) {
      if (!hash.equals(receipts.getFirst().get("payload_hash")))
        throw new ApiException(409, "OPERATION_CONFLICT", "同一操作ID对应了不同内容");
      return json.read(receipts.getFirst().get("receipt"));
    }
    var run = rows.getFirst();
    if (Boolean.TRUE.equals(run.get("cancel_requested")) || !"running".equals(run.get("status")))
      throw new ApiException(409, "RUN_STOPPED", "执行已停止，变更未提交");
    sessions.require(user, session);
    var current = travel.trip(user, session, true);
    if (versions.version(current) != body.expectedVersion())
      throw new ApiException(409, "TRIP_CONFLICT", "行程已更新，请重新整理后提交");
    if (!TRIP_FIELDS.containsAll(body.trip().keySet()))
      throw new ApiException(400, "INVALID_TRIP", "不支持的行程字段");
    body.preferences().forEach(TravelRepository::validatePreference);
    if ("new".equals(body.action())) current = new LinkedHashMap<>();
    if (!body.trip().isEmpty() || !"update".equals(body.action())) {
      current.putAll(body.trip());
      current.put("status", "cancel".equals(body.action()) ? "cancelled" : "active");
      current.putIfAbsent("_trip_id", UUID.randomUUID().toString().replace("-", ""));
      validateTrip(current);
      travel.saveTrip(user, session, current);
      if ("cancel".equals(body.action())) current = new LinkedHashMap<>();
    }
    if (!body.preferences().isEmpty()) {
      travel.lockPreferences(user);
      body.preferences().forEach((key, value) -> travel.preference(user, key, value));
    }
    var receipt =
        Map.of(
            "applied",
            true,
            "trip",
            current,
            "version",
            versions.version(current),
            "preferences_updated",
            !body.preferences().isEmpty());
    jdbc.sql(
            "INSERT INTO business_operation_receipts(user_id,request_id,operation_id,payload_hash,receipt) VALUES(:user,:request,:operation,:hash,CAST(:receipt AS jsonb))")
        .param("user", user)
        .param("request", request)
        .param("operation", body.operationId())
        .param("hash", hash)
        .param("receipt", json.write(receipt))
        .update();
    return receipt;
  }

  private static void validateTrip(Map<String, Object> trip) {
    for (String key :
        List.of("origin", "destination", "trip_purpose", "work_location", "work_schedule")) {
      Object value = trip.get(key);
      if (value != null && (!(value instanceof String text) || text.length() > 2000))
        throw new ApiException(400, "INVALID_TRIP", "行程字段无效");
    }
    Object duration = trip.get("duration_days");
    if (duration != null && (!(duration instanceof Integer n) || n < 1 || n > 90))
      throw new ApiException(400, "INVALID_TRIP", "行程天数无效");
    var start = trip.get("start_date");
    var end = trip.get("end_date");
    LocalDate s = start == null ? null : LocalDate.parse(start.toString()),
        e = end == null ? null : LocalDate.parse(end.toString());
    if (s != null
        && e != null
        && (e.isBefore(s) || java.time.temporal.ChronoUnit.DAYS.between(s, e) > 89))
      throw new ApiException(400, "INVALID_TRIP", "行程日期范围无效");
  }
}
