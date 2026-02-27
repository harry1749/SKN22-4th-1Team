from django.db import models


# 1. 고도화된 e약은요 & 제품허가 통합 테이블
class EYakInfo(models.Model):
    # 기본 정보 (제품 허가 목록 API 기반)
    item_seq = models.CharField(
        max_length=20, primary_key=True, verbose_name="품목기준코드"
    )
    item_name = models.TextField(verbose_name="제품명")  # db_index 제거
    entp_name = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="업체명"
    )

    # 상세 가이드 (e약은요 및 상세정보 API 데이터)
    efficacy = models.TextField(blank=True, null=True, verbose_name="효능")
    use_method = models.TextField(blank=True, null=True, verbose_name="사용법")
    precautions = models.TextField(blank=True, null=True, verbose_name="주의사항")
    interaction = models.TextField(blank=True, null=True, verbose_name="상호작용")
    side_effects = models.TextField(blank=True, null=True, verbose_name="부작용")

    # 메타 데이터
    item_image = models.URLField(
        max_length=500, blank=True, null=True, verbose_name="제품이미지URL"
    )
    source_updated_at = models.DateField(
        null=True, blank=True, verbose_name="식약처수정일"
    )
    last_synced_at = models.DateTimeField(auto_now=True, verbose_name="시스템동기화일")

    class Meta:
        db_table = "eyak_info"
        verbose_name = "e약은요 상세 정보"
        verbose_name_plural = "e약은요 상세 정보 목록"


# 4. 통합 의약품 정보 (e약은요 + 제품허가정보)
class UnifiedDrugInfo(models.Model):
    # 기본 정보 (e약은요 및 제품허가정보 공통)
    item_seq = models.CharField(
        max_length=20, primary_key=True, verbose_name="품목기준코드"
    )
    item_name = models.TextField(verbose_name="제품명", db_index=True)
    entp_name = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="업체명"
    )

    # 허가 정보 (DrugPermitInfo에서 획득)
    etc_otcc_name = models.CharField(
        max_length=50, blank=True, null=True, verbose_name="전문/일반"
    )
    main_ingr_eng = models.TextField(blank=True, null=True, verbose_name="주성분(영문)")
    main_ingr_kor = models.TextField(blank=True, null=True, verbose_name="주성분(한글)")

    # 상세 가이드 (EYakInfo에서 획득)
    efficacy = models.TextField(blank=True, null=True, verbose_name="효능")
    use_method = models.TextField(blank=True, null=True, verbose_name="사용법")
    precautions = models.TextField(blank=True, null=True, verbose_name="주의사항")
    interaction = models.TextField(blank=True, null=True, verbose_name="상호작용")
    side_effects = models.TextField(blank=True, null=True, verbose_name="부작용")

    # 메타 데이터
    item_image = models.URLField(
        max_length=500, blank=True, null=True, verbose_name="제품이미지URL"
    )
    source_updated_at = models.DateField(
        null=True, blank=True, verbose_name="허가일/수정일"
    )
    last_synced_at = models.DateTimeField(auto_now=True, verbose_name="시스템동기화일")

    class Meta:
        db_table = "unified_drug_info"
        verbose_name = "통합 의약품 정보"
        verbose_name_plural = "통합 의약품 정보 목록"


# 1.5. 의약품 제품 허가 정보 (검색용)
class DrugPermitInfo(models.Model):
    item_seq = models.CharField(
        max_length=50, primary_key=True, verbose_name="품목기준코드"
    )
    item_name = models.TextField(verbose_name="제품명")
    item_eng_name = models.TextField(blank=True, null=True, verbose_name="제품명(영문)")
    entp_name = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="업체명"
    )
    main_ingr_eng = models.TextField(blank=True, null=True, verbose_name="주성분(영문)")
    main_ingr_kor = models.TextField(blank=True, null=True, verbose_name="주성분(한글)")
    etc_otcc_name = models.CharField(
        max_length=50, blank=True, null=True, verbose_name="전문/일반"
    )
    source_updated_at = models.DateField(null=True, blank=True, verbose_name="허가일자")
    last_synced_at = models.DateTimeField(auto_now=True, verbose_name="시스템동기화일")

    class Meta:
        db_table = "drug_permit_info"
        managed = True
        verbose_name = "의약품 허가 정보"
        verbose_name_plural = "의약품 허가 정보 목록"


# 2. DUR 통합 마스터 테이블 (기존 유지)
class DurMaster(models.Model):
    # [기본 식별자 및 메타데이터]
    dur_seq = models.CharField(
        max_length=50, blank=True, null=True, verbose_name="DUR일련번호"
    )
    dur_type = models.CharField(
        max_length=50, db_index=True, blank=True, null=True, verbose_name="금기유형"
    )
    type_name = models.CharField(
        max_length=50, blank=True, null=True, verbose_name="금기유형명"
    )

    # [핵심 성분 정보]
    ingr_code = models.CharField(
        max_length=20, db_index=True, blank=True, null=True, verbose_name="성분코드"
    )
    ingr_kor_name = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="성분명(국문)"
    )
    ingr_eng_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        db_index=True,
        verbose_name="성분명(영문)",
    )

    # [제형 및 원문 정보]
    form_name = models.TextField(blank=True, null=True, verbose_name="제형")
    mix_type = models.CharField(
        max_length=50, blank=True, null=True, verbose_name="단일/복합"
    )
    mix_ingr = models.TextField(blank=True, null=True, verbose_name="복합성분정보")
    ori_ingr = models.TextField(blank=True, null=True, verbose_name="원문성분정보")

    # [병용금기 특화 필드]
    mixture_ingr_code = models.CharField(
        max_length=20, blank=True, null=True, verbose_name="병용금기성분코드"
    )
    mixture_ingr_kor_name = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="병용금기성분명(국문)"
    )
    mixture_ingr_eng_name = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="병용금기성분명(영문)"
    )
    mixture_mix_type = models.CharField(
        max_length=50, blank=True, null=True, verbose_name="병용단일/복합"
    )
    mixture_class = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="병용약효분류"
    )
    mixture_ori = models.TextField(blank=True, null=True, verbose_name="병용원문정보")

    # [임부/용량/기간/연령/효능중복 특화 값]
    grade = models.CharField(
        max_length=50, blank=True, null=True, verbose_name="금기등급"
    )
    max_qty = models.CharField(
        max_length=100, blank=True, null=True, verbose_name="최대투여량"
    )
    max_dosage_term = models.CharField(
        max_length=100, blank=True, null=True, verbose_name="최대투여기간"
    )
    age_base = models.CharField(
        max_length=100, blank=True, null=True, verbose_name="기준연령"
    )
    effect_code = models.CharField(
        max_length=50, blank=True, null=True, verbose_name="효능코드"
    )
    sers_name = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="효능군명"
    )

    # [공통 상세 정보]
    critical_value = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="핵심주의값(통합)"
    )
    prohbt_content = models.TextField(blank=True, null=True, verbose_name="금기내용")
    remark = models.TextField(blank=True, null=True, verbose_name="비고")
    class_name = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="효능군/계열"
    )
    notification_date = models.DateField(null=True, blank=True, verbose_name="공고일자")
    del_yn = models.CharField(max_length=10, default="정상", verbose_name="삭제여부")

    last_synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dur_master"
        indexes = [
            models.Index(fields=["dur_type", "ingr_code"]),
        ]


# 3. 사용자 건강 정보 프로필
from django.contrib.auth.models import User


class UserProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile", verbose_name="사용자"
    )
    current_medications = models.TextField(
        blank=True, null=True, verbose_name="복용 중인 약"
    )
    allergies = models.TextField(blank=True, null=True, verbose_name="알러지")
    chronic_diseases = models.TextField(blank=True, null=True, verbose_name="기저질환")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="수정일")

    class Meta:
        db_table = "user_profile"
        verbose_name = "사용자 프로필"
        verbose_name_plural = "사용자 프로필 목록"
