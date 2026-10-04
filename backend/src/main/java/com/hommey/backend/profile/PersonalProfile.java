package com.hommey.backend.profile;

import com.fasterxml.jackson.annotation.JsonIgnore;
import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.*;

public record PersonalProfile(
    @NotNull @Min(1) @Max(1) Integer schemaVersion,
    @Valid @NotNull BasicInfo basicInfo,
    @Valid @NotNull PolicyIdentity policyIdentity,
    @Valid @NotNull Funding funding,
    @Valid @NotNull Settlement settlement) {
  public PersonalProfile {
    if (schemaVersion == null) schemaVersion = 1;
    if (basicInfo == null)
      basicInfo = new BasicInfo(null, "重庆大学", null, null, null, null, null, null, null);
    if (policyIdentity == null)
      policyIdentity = new PolicyIdentity(null, null, null, null, null, null);
    if (funding == null) funding = new Funding(null, List.of());
    if (settlement == null) settlement = new Settlement(null);
  }

  public record BasicInfo(
      @Size(max = 120) String realName,
      @Size(max = 120) String institution,
      @Size(max = 120) String department,
      @Pattern(regexp = "staff|student|external|other") String personnelCategory,
      @Size(min = 1, max = 64) String employeeNumber,
      @Size(min = 1, max = 64) String studentNumber,
      @Pattern(regexp = "male|female|other|undisclosed") String gender,
      LocalDate birthDate,
      @Size(max = 120) String nationality) {
    @AssertTrue
    @JsonIgnore
    public boolean isBirthDateValid() {
      return birthDate == null
          || (!birthDate.isAfter(LocalDate.now(ZoneId.of("Asia/Shanghai")))
              && birthDate.getYear() >= 1900);
    }
  }

  public record PolicyIdentity(
      @Pattern(regexp = "senior|associate_senior|intermediate|junior|none")
          String professionalTitleLevel,
      @Min(1) @Max(13) Integer professionalPositionGrade,
      @Min(1) @Max(10) Integer staffGrade,
      @Pattern(regexp = "provincial_ministerial|department_bureau|other") String administrativeRank,
      @Pattern(regexp = "academician|equivalent|none") String academicianStatus,
      Boolean nationallyRecognizedExpert) {
    @AssertTrue
    @JsonIgnore
    public boolean isPositionValid() {
      return !"none".equals(professionalTitleLevel) || professionalPositionGrade == null;
    }

    public boolean hasValues() {
      return professionalTitleLevel != null
          || professionalPositionGrade != null
          || staffGrade != null
          || administrativeRank != null
          || academicianStatus != null
          || nationallyRecognizedExpert != null;
    }
  }

  public record FundingProject(
      @Size(min = 1, max = 64) String id,
      @Size(max = 120) String name,
      @Size(min = 1, max = 64) String financialProjectCode,
      @Pattern(regexp = "research|non_research") String fundingCategory,
      @Pattern(regexp = "vertical|horizontal") String researchType,
      @Pattern(regexp = "national_science_technology|national_social_science|other")
          String programType,
      @Pattern(regexp = "fiscal|non_fiscal") String fundingSource,
      Boolean isMilitaryProject,
      @Pattern(regexp = "principal|member|handler") String userProjectRole) {
    @AssertTrue
    @JsonIgnore
    public boolean isResearchValid() {
      return "research".equals(fundingCategory)
          || (researchType == null && programType == null && isMilitaryProject == null);
    }

    public FundingProject withId() {
      return id != null
          ? this
          : new FundingProject(
              UUID.randomUUID().toString(),
              name,
              financialProjectCode,
              fundingCategory,
              researchType,
              programType,
              fundingSource,
              isMilitaryProject,
              userProjectRole);
    }
  }

  public record Funding(
      @Size(min = 1, max = 64) String defaultProjectId,
      @Valid @Size(max = 20) List<FundingProject> projects) {
    public Funding {
      if (projects == null) projects = List.of();
    }

    @AssertTrue
    @JsonIgnore
    public boolean isDefaultValid() {
      var ids = projects.stream().map(FundingProject::id).filter(Objects::nonNull).toList();
      return new HashSet<>(ids).size() == ids.size()
          && (defaultProjectId == null || ids.contains(defaultProjectId));
    }
  }

  public record Settlement(Boolean hasOfficialCard) {}

  @AssertTrue
  @JsonIgnore
  public boolean isPersonnelValid() {
    String category = basicInfo.personnelCategory();
    if ((category == null || category.equals("student") || category.equals("other"))
        && policyIdentity.hasValues()) return false;
    return "staff".equals(category)
        || (policyIdentity.staffGrade() == null && policyIdentity.administrativeRank() == null);
  }

  public PersonalProfile withProjectIds() {
    return new PersonalProfile(
        schemaVersion,
        basicInfo,
        policyIdentity,
        new Funding(
            funding.defaultProjectId(),
            funding.projects().stream().map(FundingProject::withId).toList()),
        settlement);
  }

  public static PersonalProfile defaults() {
    return new PersonalProfile(1, null, null, null, null);
  }
}
