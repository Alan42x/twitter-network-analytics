"""Shared entry point for local Spark and EMR on EC2; no Python UDFs."""
import argparse
from pyspark.sql import SparkSession, functions as F
from pyspark import StorageLevel


def compute(spark, input_path):
    # Match the oracle's LF-delimited records, including CRLF and embedded CR whitespace.
    raw = spark.read.option("lineSep", "\n").text(input_path)
    pattern = r"^\s*([0-9]{1,20})\s+([0-9]{1,20})\s*$"
    parsed = raw.select(
        "value",
        F.regexp_replace(F.regexp_extract("value", pattern, 1), "^0+", "").alias("source"),
        F.regexp_replace(F.regexp_extract("value", pattern, 2), "^0+", "").alias("target"),
    ).persist(StorageLevel.MEMORY_AND_DISK)
    blank = F.col("value").rlike(r"^\s*$")
    valid = (F.col("source") != "") & (F.col("target") != "")

    def total(condition):
        return F.coalesce(F.sum(F.when(condition, 1).otherwise(0)), F.lit(0)).cast("long")

    audit = parsed.agg(
        F.count("*").alias("raw_lines"), total(blank).alias("blank_rows"),
        total(valid).alias("valid_rows"), total(~blank & ~valid).alias("invalid_rows"),
        total(valid & (F.col("source") == F.col("target"))).alias("self_loop_rows"),
    ).first().asDict()
    valid_rows = parsed.filter(valid).select("source", "target")
    # Keep every valid endpoint, including users whose only rows were self-loops.
    endpoints = valid_rows.select(F.col("source").alias("user_id")).union(
        valid_rows.select(F.col("target").alias("user_id"))).distinct()
    edges = valid_rows.filter(F.col("source") != F.col("target")).dropDuplicates(
        ["source", "target"]).persist(StorageLevel.MEMORY_AND_DISK)
    incoming = edges.groupBy("target").count().select(
        F.col("target").alias("user_id"), F.col("count").alias("followers"))
    outgoing = edges.groupBy("source").count().select(
        F.col("source").alias("user_id"), F.col("count").alias("followees"))
    users = endpoints.join(incoming, "user_id", "left").join(outgoing, "user_id", "left").fillna(
        0, subset=["followers", "followees"]).persist(StorageLevel.MEMORY_AND_DISK)
    unique_edges = edges.count()
    totals = users.agg(F.count("*").alias("total_users"),
                      F.coalesce(F.sum("followers"), F.lit(0)).alias("followers_sum"),
                      F.coalesce(F.sum("followees"), F.lit(0)).alias("followees_sum")).first().asDict()
    audit.update(totals, unique_edges=unique_edges,
                 duplicate_edges=audit["valid_rows"] - audit["self_loop_rows"] - unique_edges)
    if audit["followers_sum"] != unique_edges or audit["followees_sum"] != unique_edges:
        raise RuntimeError("Edge/count conservation failed")
    # JVM expressions avoid driver-side dataset collection and Python workers.
    summary = spark.range(1).select(*[F.lit(value).alias(key) for key, value in audit.items()])
    return users, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True, help="New run directory; existing output is rejected")
    parser.add_argument("--partitions", type=int, default=4)
    args = parser.parse_args()
    if args.partitions < 1:
        parser.error("partitions must be positive")
    spark = SparkSession.builder.appName("CS5224-Twitter-Degree-Analytics").getOrCreate()
    try:
        spark.conf.set("spark.sql.shuffle.partitions", str(args.partitions))
        users, summary = compute(spark, args.input)
        users.repartition(args.partitions).write.mode("errorifexists").json(args.output.rstrip("/") + "/users")
        summary.coalesce(1).write.mode("errorifexists").json(args.output.rstrip("/") + "/summary")
        summary.show(truncate=False)
        users.filter(F.col("user_id").isin("214328887", "107830991")).orderBy("user_id").show(truncate=False)
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
